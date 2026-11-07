"""End-to-End Integration Test for Argus Desktop Architecture."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from argus.api.server import create_app
from argus.api.session import HistoryManager
from argus.runtime.controller import AttemptRecord, RecoveryOutcome


def test_end_to_end_desktop_workflow():
    """Verify complete end-to-end flow: dispatch task -> stream SSE -> review diff -> apply patch -> check history."""
    with tempfile.TemporaryDirectory() as tmp_root:
        root = Path(tmp_root).resolve()
        target_file = root / "src" / "service.py"
        target_file.parent.mkdir(parents=True, exist_ok=True)
        original_code = "def get_status(): return 'offline'\n"
        target_file.write_text(original_code, encoding="utf-8")

        test_token = "e2e-master-token-999"
        app = create_app(workspace=str(root), token=test_token)
        client = TestClient(app)

        # 1. Healthcheck probe (readiness check)
        health_resp = client.get("/api/v1/health")
        assert health_resp.status_code == 200
        assert health_resp.json()["status"] == "ok"

        # 2. Mock recovery loop execution
        patched_code = "def get_status(): return 'online'\n"
        expected_diff = "--- a/src/service.py\n+++ b/src/service.py\n@@ -1 +1 @@\n-def get_status(): return 'offline'\n+def get_status(): return 'online'\n"

        def mock_controller_run(target_file, test_command, test_file=None, on_attempt_start=None, on_attempt_end=None):
            if on_attempt_start:
                on_attempt_start(1, 4)
            record = AttemptRecord(
                attempt=1,
                duration_ms=85.0,
                prompt_tokens=40,
                completion_tokens=15,
                cost_usd=0.00008,
                tier_used=1,
                patch_success=True,
                test_passed=True,
            )
            if on_attempt_end:
                on_attempt_end(record)
            return RecoveryOutcome(
                status="SUCCESS",
                total_attempts=1,
                total_cost_usd=0.00008,
                total_duration_ms=110.0,
                unified_diff=expected_diff,
                patched_content=patched_code,
                target_file=str(target_file),
            )

        with patch("argus.api.recovery.RecoveryController.run", side_effect=mock_controller_run):
            # 3. Post task to run
            run_resp = client.post(
                "/api/v1/recovery/run",
                headers={"Authorization": f"Bearer {test_token}"},
                json={
                    "target_file": "src/service.py",
                    "test_command": "pytest tests/test_service.py",
                    "model": "argus/auto",
                    "max_attempts": 4,
                    "budget": 0.05,
                },
            )
            assert run_resp.status_code == 200
            task_id = run_resp.json()["task_id"]

            # 4. Stream SSE events
            stream_resp = client.get(
                f"/api/v1/recovery/stream/{task_id}",
                headers={"Authorization": f"Bearer {test_token}"},
            )
            assert stream_resp.status_code == 200
            events = []
            for line in stream_resp.iter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

            assert any(e["type"] == "ATTEMPT_START" for e in events)
            assert any(e["type"] == "ATTEMPT_END" for e in events)
            complete_event = next(e for e in events if e["type"] == "RECOVERY_COMPLETE")
            assert complete_event["payload"]["status"] == "SUCCESS"

            # 5. Review Diff
            diff_resp = client.get(
                f"/api/v1/recovery/diff/{task_id}",
                headers={"Authorization": f"Bearer {test_token}"},
            )
            assert diff_resp.status_code == 200
            assert "get_status(): return 'online'" in diff_resp.json()["unified_diff"]

            # 6. Approve & Apply Patch
            apply_resp = client.post(
                "/api/v1/recovery/apply",
                headers={"Authorization": f"Bearer {test_token}"},
                json={"task_id": task_id, "target_file": "src/service.py"},
            )
            assert apply_resp.status_code == 200
            assert apply_resp.json()["status"] == "applied"

            # 7. Verify file on disk
            assert target_file.read_text(encoding="utf-8") == patched_code
            assert (root / ".argus" / "last_run.diff").exists()

            # 8. Record in history and query history endpoint
            HistoryManager.record_run(
                workspace=root,
                run_record={
                    "task_id": task_id,
                    "target_file": "src/service.py",
                    "status": "SUCCESS",
                    "total_attempts": 1,
                    "cost_usd": 0.00008,
                },
            )

            hist_resp = client.get(
                "/api/v1/session/history",
                headers={"Authorization": f"Bearer {test_token}"},
            )
            assert hist_resp.status_code == 200
            history = hist_resp.json()["history"]
            assert len(history) == 1
            assert history[0]["task_id"] == task_id
