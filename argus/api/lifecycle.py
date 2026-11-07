"""Backend Supervision, Port Discovery, and Process Lifecycle.

Implements ADR-002:
- Dynamic ephemeral port negotiation.
- Watchdog heartbeat supervision (15s missed ping auto-termination).
- Session file tracking (.argus/session.json) and cleanup.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("argus.api.lifecycle")


def find_free_port(preferred_port: int = 8000) -> int:
    """Find an available TCP port on loopback.
    
    Tries preferred_port first. If occupied, asks the OS kernel for an ephemeral port.
    """
    # 1. Check if preferred port is free
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", preferred_port))
            return preferred_port
        except OSError:
            pass

    # 2. Ephemeral port from kernel
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HeartbeatWatchdog:
    """Monitors client liveness and shuts down the server if client window closes."""

    def __init__(
        self,
        timeout_seconds: float = 15.0,
        check_interval: float = 2.0,
        on_timeout: Optional[Callable[[], None]] = None,
        is_busy_fn: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.check_interval = check_interval
        self.on_timeout = on_timeout
        self.is_busy_fn = is_busy_fn
        self.last_heartbeat = time.monotonic()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def record_heartbeat(self) -> None:
        """Update last heartbeat timestamp."""
        with self._lock:
            self.last_heartbeat = time.monotonic()

    def start(self) -> None:
        """Start watchdog background monitoring thread."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self.last_heartbeat = time.monotonic()
            self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name="argus-watchdog")
            self._thread.start()

    def stop(self) -> None:
        """Stop watchdog thread."""
        with self._lock:
            self._running = False

    def _monitor_loop(self) -> None:
        while self._running:
            time.sleep(self.check_interval)
            with self._lock:
                elapsed = time.monotonic() - self.last_heartbeat

            if elapsed > self.timeout_seconds:
                # If engine is actively executing tests, don't kill prematurely
                if self.is_busy_fn and self.is_busy_fn():
                    logger.debug("Heartbeat overdue (%0.1fs) but recovery engine is busy. Deferring exit.", elapsed)
                    continue

                logger.warning("Heartbeat timed out after %0.1fs. Initiating process shutdown.", elapsed)
                if self.on_timeout:
                    try:
                        self.on_timeout()
                    except Exception as ex:
                        logger.error("Error in on_timeout callback: %s", ex)
                
                # Terminate daemon cleanly
                os._exit(0)


class SessionFileManager:
    """Manages active session metadata file in .argus/session.json."""

    def __init__(self, workspace: Path | str) -> None:
        self.workspace = Path(workspace).resolve()
        self.dot_argus = self.workspace / ".argus"
        self.session_file = self.dot_argus / "session.json"

    def write_session(self, port: int, pid: int, token_preview: str = "") -> Path:
        """Record session metadata to disk."""
        self.dot_argus.mkdir(parents=True, exist_ok=True)
        data = {
            "port": port,
            "pid": pid,
            "workspace": str(self.workspace),
            "created_at": time.time(),
            "token_preview": token_preview[:6] + "..." if token_preview else "",
        }
        self.session_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return self.session_file

    def cleanup(self) -> None:
        """Remove session file on exit."""
        try:
            if self.session_file.exists():
                self.session_file.unlink()
        except Exception:
            pass
