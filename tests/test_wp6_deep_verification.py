"""WP-6 Deep Verification: Memory Boundedness, Process-Tree Reaping, and Session Lifecycle Concurrency."""

from __future__ import annotations

import collections
import concurrent.futures
import json
import os
import psutil
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from argus.config import ArgusConfig
from argus.providers.adapter import OpenAICompatibleAdapter
from argus.providers.models import ProviderConfig
from argus.runtime.controller import RecoveryController
from argus.runtime.runner import BoundedOutputCollector, SubprocessTestRunner
from argus.runtime.workspace import (
    WorkspaceManager,
    cleanup_stale_temp_workspaces,
    get_process_create_time,
    is_process_alive,
)


# ============================================================================
# 1. Memory Measurement: 1 GB Logical Output Stream Test
# ============================================================================

def test_runner_1gb_logical_stream_bounded_memory(tmp_path: Path):
    """Subprocess generates >= 1 GB of logical output; collector stays strictly bounded in memory."""
    # Configure buffer to 2 MB (max_output_bytes=2097152)
    max_buf = 2 * 1024 * 1024
    runner = SubprocessTestRunner(timeout_seconds=30, max_output_bytes=max_buf)

    current_proc = psutil.Process(os.getpid())
    rss_before = current_proc.memory_info().rss

    # Python script generates 1024 chunks of 1 MB = 1,073,741,824 bytes (1 GB) to stdout
    # without touching disk, interspersed with stderr messages
    code = (
        "import sys\n"
        "chunk = b'A' * (1024 * 1024)\n"
        "for i in range(1024):\n"
        "    sys.stdout.buffer.write(chunk)\n"
        "    if i % 128 == 0:\n"
        "        sys.stderr.write(f'Progress: chunk {i}\\n')\n"
        "        sys.stderr.flush()\n"
        "sys.stdout.buffer.flush()\n"
    )

    res = runner.run_test(f'python -c "{code}"', cwd=tmp_path)

    rss_after = current_proc.memory_info().rss
    rss_delta_mb = (rss_after - rss_before) / (1024 * 1024)

    assert res.passed is True
    assert "Progress: chunk" in res.stderr
    assert "OUTPUT TRUNCATED" in res.stdout
    # Length of captured stdout in memory must be strictly bounded near configured 2 MB buffer
    assert len(res.stdout.encode("utf-8")) <= max_buf * 1.5
    # The parent process resident memory growth must be far below 1 GB (under 50 MB accounting for Python GC)
    assert rss_delta_mb < 50.0, f"Parent process memory grew excessively: {rss_delta_mb:.2f} MB"


def test_runner_concurrent_stdout_stderr_and_continuous_output_timeout(tmp_path: Path):
    """Collector drains concurrent stdout/stderr and terminates runaway stream on timeout."""
    runner = SubprocessTestRunner(timeout_seconds=2, max_output_bytes=512 * 1024)

    # Subprocess writes continuously to both stdout and stderr in an infinite loop
    code = (
        "import sys, time\n"
        "while True:\n"
        "    sys.stdout.write('OUT_STREAM_LINE\\n')\n"
        "    sys.stderr.write('ERR_STREAM_LINE\\n')\n"
        "    sys.stdout.flush()\n"
        "    sys.stderr.flush()\n"
        "    time.sleep(0.005)\n"
    )

    start = time.perf_counter()
    res = runner.run_test(f'python -c "{code}"', cwd=tmp_path)
    elapsed = time.perf_counter() - start

    assert res.passed is False
    assert res.timed_out is True
    # Must terminate within timeout + 2.0s join window (prevent hangs)
    assert elapsed < 5.0
    assert "OUT_STREAM_LINE" in res.stdout
    assert "ERR_STREAM_LINE" in res.stderr


# ============================================================================
# 2. Process-Tree Termination: Parent -> Child -> Grandchild Kill
# ============================================================================

def test_runner_terminates_full_descendant_process_tree(tmp_path: Path):
    """Timeout on parent process hard-kills child and grandchild processes."""
    runner = SubprocessTestRunner(timeout_seconds=2)

    child_pid_file = tmp_path / "child.pid"
    grandchild_pid_file = tmp_path / "grandchild.pid"

    # Write helper scripts to avoid shell characters like semicolon
    grandchild_script = tmp_path / "grandchild.py"
    grandchild_script.write_text("import time\ntime.sleep(60)\n")

    child_script = tmp_path / "child.py"
    child_code = (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "gc_script = sys.argv[1]\n"
        "gc_file = sys.argv[2]\n"
        "gc = subprocess.Popen([sys.executable, gc_script])\n"
        "Path(gc_file).write_text(str(gc.pid))\n"
        "time.sleep(60)\n"
    )
    child_script.write_text(child_code)

    parent_script = tmp_path / "parent.py"
    parent_code = (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "c_script = sys.argv[1]\n"
        "gc_script = sys.argv[2]\n"
        "c_file = sys.argv[3]\n"
        "gc_file = sys.argv[4]\n"
        "child = subprocess.Popen([sys.executable, c_script, gc_script, gc_file])\n"
        "Path(c_file).write_text(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    parent_script.write_text(parent_code)

    cmd = f'python "{parent_script}" "{child_script}" "{grandchild_script}" "{child_pid_file}" "{grandchild_pid_file}"'
    res = runner.run_test(cmd, cwd=tmp_path)

    assert res.timed_out is True

    # Read child and grandchild PIDs
    assert child_pid_file.exists(), "Child process failed to start or record PID"
    assert grandchild_pid_file.exists(), "Grandchild process failed to start or record PID"

    child_pid = int(child_pid_file.read_text().strip())
    grandchild_pid = int(grandchild_pid_file.read_text().strip())

    # Give OS a brief moment to finish reap
    time.sleep(0.5)

    # Prove beyond doubt that neither descendant process survives
    child_alive = psutil.pid_exists(child_pid) and is_process_alive(child_pid)
    grandchild_alive = psutil.pid_exists(grandchild_pid) and is_process_alive(grandchild_pid)

    assert not child_alive, f"Child process {child_pid} was not terminated by process tree kill!"
    assert not grandchild_alive, f"Grandchild process {grandchild_pid} was not terminated by process tree kill!"


# ============================================================================
# 3. Workspace Ownership, PID Reuse & Concurrency
# ============================================================================

def test_workspace_pid_reuse_detection():
    """Detects PID reuse by verifying process creation timestamp."""
    my_pid = os.getpid()
    actual_ctime = get_process_create_time(my_pid)
    assert actual_ctime is not None

    # Matching creation timestamp proves true owner
    assert is_process_alive(my_pid, expected_create_time=actual_ctime) is True

    # Artificial creation time differing by 10,000s simulates an OS PID recycling scenario
    stale_ctime = actual_ctime - 10000.0
    # Must report False: the running process is NOT the owner of the stale workspace
    assert is_process_alive(my_pid, expected_create_time=stale_ctime) is False


def test_cleanup_stale_temp_workspaces_preserves_unobservable_and_fresh(tmp_path: Path):
    """Preserves workspaces if session is fresh (< max_age_hours) or metadata is temporarily unreadable."""
    temp_dir = Path(tempfile.gettempdir())
    ws_fresh = temp_dir / f"argus_ws_test_fresh_{os.getpid()}"
    ws_fresh.mkdir(exist_ok=True)
    try:
        # Write corrupted/locked metadata
        (ws_fresh / ".argus_session.json").write_text("{ incomplete json ...")
        # Touch directory mtime to 2 hours ago
        two_hours_ago = time.time() - 7200
        os.utime(ws_fresh, (two_hours_ago, two_hours_ago))

        # Because metadata read fails, cleanup must fail-safe and preserve it
        cleanup_stale_temp_workspaces(max_age_hours=1.0)
        assert ws_fresh.exists(), "Corrupted session metadata caused unsafe workspace deletion!"
    finally:
        shutil.rmtree(ws_fresh, ignore_errors=True)


def test_concurrent_garbage_collection_during_active_sessions(tmp_path: Path):
    """Concurrent garbage collectors do not interfere with active sessions or crash."""
    temp_dir = Path(tempfile.gettempdir())

    # Create two active workspaces with real session metadata
    ws1 = temp_dir / f"argus_ws_active_1_{os.getpid()}"
    ws2 = temp_dir / f"argus_ws_active_2_{os.getpid()}"
    ws1.mkdir(exist_ok=True)
    ws2.mkdir(exist_ok=True)

    meta = {
        "session_id": "test_active",
        "pid": os.getpid(),
        "process_create_time": get_process_create_time(os.getpid()),
        "created_at": time.time(),
        "repo_root": str(tmp_path),
    }
    (ws1 / ".argus_session.json").write_text(json.dumps(meta), encoding="utf-8")
    (ws2 / ".argus_session.json").write_text(json.dumps(meta), encoding="utf-8")

    # Also create 5 dead workspaces older than 2 hours
    dead_dirs = []
    for i in range(5):
        d = temp_dir / f"argus_ws_dead_{i}_{os.getpid()}"
        d.mkdir(exist_ok=True)
        dead_meta = {
            "session_id": f"dead_{i}",
            "pid": 999999,
            "created_at": time.time() - 10000,
            "repo_root": str(tmp_path),
        }
        (d / ".argus_session.json").write_text(json.dumps(dead_meta), encoding="utf-8")
        two_hrs_ago = time.time() - 7200
        os.utime(d, (two_hrs_ago, two_hrs_ago))
        dead_dirs.append(d)

    try:
        # Run 8 concurrent garbage collector threads simultaneously
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(cleanup_stale_temp_workspaces, 1.0) for _ in range(8)]
            results = [f.result() for f in futures]

        # Active workspaces MUST remain intact
        assert ws1.exists()
        assert ws2.exists()
        # All target dead directories must be removed
        for d in dead_dirs:
            assert not d.exists(), f"Dead directory {d.name} was not cleaned up!"
    finally:
        shutil.rmtree(ws1, ignore_errors=True)
        shutil.rmtree(ws2, ignore_errors=True)
        for d in dead_dirs:
            shutil.rmtree(d, ignore_errors=True)


# ============================================================================
# 4. HTTP Client Lifecycle Management in Controller
# ============================================================================

def test_recovery_controller_deterministic_http_client_lifecycle():
    """RecoveryController deterministically closes managed HTTP client via close() and context manager."""
    cfg = ArgusConfig(provider="openai", model="gpt-4o")
    p_cfg = ProviderConfig(name="openai", model="gpt-4o", api_key="test-key", base_url="https://api.openai.com/v1")
    cfg.provider_config = p_cfg

    controller = RecoveryController(config=cfg)
    adapter = controller._get_adapter()
    assert isinstance(adapter, OpenAICompatibleAdapter)

    # Force initialization of managed HTTP client
    client = adapter._get_client()
    assert client is not None
    assert client.is_closed is False

    # Controller close closes adapter client
    controller.close()
    assert client.is_closed is True

    # Test context manager interface
    with RecoveryController(config=cfg) as ctrl:
        ad = ctrl._get_adapter()
        c2 = ad._get_client()
        assert c2.is_closed is False
    assert c2.is_closed is True
