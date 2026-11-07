"""Tests for Phase 3: Recovery Controller API & Server-Sent Events (SSE) Streaming."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from argus.api.server import create_app
from argus.runtime.controller import AttemptRecord, RecoveryOutcome


def test_recovery_run_auth_and_path_validation():
    """Verify auth gating and directory traversal prevention on /recovery/run."""
    with tempfile.TemporaryDirectory() as tmp_root:
        root = Path(tmp_root).resolve()
        test_token = "valid-secret-token-333"
        app = create_app(workspace=str(root), token=test_token)
        client = TestClient(app)

        # 1. No token -> 401
        resp = client.post(
            "/api/v1/recovery/run",
            json={"target_file": "file.py", "test_command": "pytest"},
        )
        assert resp.status_code == 401

        # 2. Path traversal -> 403
        resp = client.post(
            "/api/v1/recovery/run",
            headers={"Authorization": f"Bearer {test_token}"},
            json={"target_file": "../../etc/passwd", "test_command": "pytest"},
        )
        assert resp.status_code == 403


def test_recovery_dispatch_and_stream():
    """Verify that dispatching a task initiates background execution and streams SSE events."""
    with tempfile.TemporaryDirectory() as tmp_root:
        root = Path(tmp_root).resolve()
        target_file = root / "src" / "calc.py"
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text("def add(a, b): return a - b\n", encoding="utf-8")

        test_token = "secret-stream-token"
        app = create_app(workspace=str(root), token=test_token)
        client = TestClient(app)

        # Mock RecoveryController.run to return a deterministic outcome and fire callbacks
        fake_record = AttemptRecord(
            attempt=1,
            duration_ms=120.0,
            prompt_tokens=50,
            completion_tokens=20,
            cost_usd=0.0001,
            tier_used=1,
            patch_success=True,
            test_passed=True,
        )
        fake_outcome = RecoveryOutcome(
            status="SUCCESS",
            total_attempts=1,
            total_cost_usd=0.0001,
            total_duration_ms=150.0,
            unified_diff="--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1 +1 @@\n-def add(a, b): return a - b\n+def add(a, b): return a + b\n",
            message="Bug resolved in attempt 1",
            target_file=str(target_file),
        )

        def mock_run(target_file, test_command, test_file=None, on_attempt_start=None, on_attempt_end=None):
            if on_attempt_start:
                on_attempt_start(1, 4)
            if on_attempt_end:
                on_attempt_end(fake_record)
            return fake_outcome

        with patch("argus.api.recovery.RecoveryController.run", side_effect=mock_run):
            # 1. Post run request
            resp = client.post(
                "/api/v1/recovery/run",
                headers={"Authorization": f"Bearer {test_token}"},
                json={
                    "target_file": "src/calc.py",
                    "test_command": "pytest tests/test_calc.py",
                    "model": "argus/auto",
                    "max_attempts": 4,
                    "budget": 0.05,
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "task_id" in data
            task_id = data["task_id"]

            # 2. Connect to SSE stream
            stream_resp = client.get(
                f"/api/v1/recovery/stream/{task_id}",
                headers={"Authorization": f"Bearer {test_token}"},
            )
            assert stream_resp.status_code == 200
            assert "text/event-stream" in stream_resp.headers["content-type"]

            # Parse lines from stream
            events = []
            for line in stream_resp.iter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

            assert len(events) >= 3
            types = [e["type"] for e in events]
            assert "ATTEMPT_START" in types
            assert "ATTEMPT_END" in types
            assert "RECOVERY_COMPLETE" in types

            # Check outcome
            comp = next(e for e in events if e["type"] == "RECOVERY_COMPLETE")
            assert comp["payload"]["status"] == "SUCCESS"
            assert "unified_diff" in comp["payload"]
