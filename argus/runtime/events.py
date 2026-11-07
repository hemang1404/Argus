"""Machine-Readable JSON Lines IPC Event Protocol for Argus.

Implements Stage B Architectural Requirement (DISC-03):
- Versioned, structured streaming events emitted across standard output.
- Eliminates brittle text heuristics and regex sniffing in frontend TUIs.
- Guaranteed single-line JSON with immediate stream flushing.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional, TextIO


class EventType(str, Enum):
    SESSION_INIT = "SESSION_INIT"
    ATTEMPT_START = "ATTEMPT_START"
    INFERENCE_DONE = "INFERENCE_DONE"
    PATCH_RECONCILED = "PATCH_RECONCILED"
    TEST_RESULT = "TEST_RESULT"
    OUTCOME = "OUTCOME"


PROTOCOL_VERSION = "1.0"


@dataclass
class ArgusEvent:
    """Standardized versioned event envelope for JSON Lines IPC."""
    event: str
    data: Dict[str, Any]
    version: str = PROTOCOL_VERSION

    def to_json(self) -> str:
        """Serialize event to single-line JSON string without internal newlines."""
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> Optional[ArgusEvent]:
        """Parse event from JSON line string; returns None if malformed."""
        try:
            raw = json.loads(line.strip())
            if isinstance(raw, dict) and "event" in raw and "data" in raw:
                return cls(
                    event=str(raw["event"]),
                    data=raw.get("data", {}),
                    version=str(raw.get("version", PROTOCOL_VERSION)),
                )
        except Exception:
            pass
        return None


class JsonLinesEmitter:
    """Streams versioned Argus events as line-delimited JSON objects to an output stream."""

    def __init__(self, stream: Optional[TextIO] = None) -> None:
        self.stream = stream or sys.stdout

    def emit(self, event_type: EventType | str, data: Dict[str, Any]) -> None:
        """Write single-line JSON event to stream and flush immediately."""
        evt_name = event_type.value if isinstance(event_type, EventType) else str(event_type)
        envelope = ArgusEvent(event=evt_name, data=data)
        line = envelope.to_json()
        self.stream.write(line + "\n")
        self.stream.flush()

    # Specialized event factory methods
    def session_init(
        self,
        run_id: str,
        target_file: str,
        test_command: str,
        baseline_hash: str,
        model: str,
        max_attempts: int,
        budget_limit_usd: float,
    ) -> None:
        self.emit(
            EventType.SESSION_INIT,
            {
                "run_id": run_id,
                "target_file": target_file,
                "test_command": test_command,
                "baseline_hash": baseline_hash,
                "model": model,
                "max_attempts": max_attempts,
                "budget_limit_usd": budget_limit_usd,
            },
        )

    def attempt_start(self, attempt: int, max_attempts: int, model: str) -> None:
        self.emit(
            EventType.ATTEMPT_START,
            {
                "attempt": attempt,
                "max_attempts": max_attempts,
                "model": model,
            },
        )

    def inference_done(
        self,
        attempt: int,
        duration_ms: float,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        model: str,
    ) -> None:
        self.emit(
            EventType.INFERENCE_DONE,
            {
                "attempt": attempt,
                "duration_ms": duration_ms,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": cost_usd,
                "model": model,
            },
        )

    def patch_reconciled(
        self,
        attempt: int,
        patch_success: bool,
        tier_used: Optional[int],
        ast_valid: bool,
        error_summary: str = "",
    ) -> None:
        self.emit(
            EventType.PATCH_RECONCILED,
            {
                "attempt": attempt,
                "patch_success": patch_success,
                "tier_used": tier_used,
                "ast_valid": ast_valid,
                "error_summary": error_summary,
            },
        )

    def test_result(
        self,
        attempt: int,
        passed: bool,
        duration_ms: float,
        error_summary: str = "",
    ) -> None:
        self.emit(
            EventType.TEST_RESULT,
            {
                "attempt": attempt,
                "passed": passed,
                "duration_ms": duration_ms,
                "error_summary": error_summary,
            },
        )

    def outcome(
        self,
        status: str,
        run_id: str,
        verified: bool,
        total_attempts: int,
        total_cost_usd: float,
        total_duration_ms: float,
        target_file: str,
        unified_diff: str = "",
        message: str = "",
    ) -> None:
        self.emit(
            EventType.OUTCOME,
            {
                "status": status,
                "run_id": run_id,
                "verified": verified,
                "total_attempts": total_attempts,
                "total_cost_usd": total_cost_usd,
                "total_duration_ms": total_duration_ms,
                "target_file": target_file,
                "unified_diff": unified_diff,
                "message": message,
            },
        )
