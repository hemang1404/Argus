"""Tests for Phase 5: Session History, Spend Telemetry, and Preset Management."""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from argus.api.server import create_app
from argus.api.session import HistoryManager


def test_session_spend_telemetry():
    """Verify that /session/spend returns current spend and budget caps."""
    test_token = "spend-token-123"
    app = create_app(workspace=".", token=test_token)
    client = TestClient(app)

    # 1. No auth -> 401
    resp = client.get("/api/v1/session/spend")
    assert resp.status_code == 401

    # 2. Authenticated -> 200 with spend stats
    resp = client.get(
        "/api/v1/session/spend",
        headers={"Authorization": f"Bearer {test_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "session_spend_usd" in data
    assert "session_budget_limit_usd" in data
    assert "task_budget_limit_usd" in data


def test_session_history_recording_and_retrieval():
    """Verify that history manager logs and retrieves run records."""
    with tempfile.TemporaryDirectory() as tmp_root:
        root = Path(tmp_root).resolve()
        test_token = "history-token-456"
        app = create_app(workspace=str(root), token=test_token)
        client = TestClient(app)

        # Record a test run
        HistoryManager.record_run(
            workspace=root,
            run_record={
                "task_id": "test-task-1",
                "target_file": "src/app.py",
                "status": "SUCCESS",
                "total_attempts": 2,
                "cost_usd": 0.00045,
            },
        )

        resp = client.get(
            "/api/v1/session/history",
            headers={"Authorization": f"Bearer {test_token}"},
        )
        assert resp.status_code == 200
        history = resp.json()["history"]
        assert len(history) == 1
        assert history[0]["task_id"] == "test-task-1"
        assert history[0]["status"] == "SUCCESS"


def test_presets_listing_and_export():
    """Verify listing presets and exporting a configuration file to workspace."""
    with tempfile.TemporaryDirectory() as tmp_root:
        root = Path(tmp_root).resolve()
        test_token = "preset-token-789"
        app = create_app(workspace=str(root), token=test_token)
        client = TestClient(app)

        # 1. List presets
        resp = client.get(
            "/api/v1/session/presets",
            headers={"Authorization": f"Bearer {test_token}"},
        )
        assert resp.status_code == 200
        presets = resp.json()["presets"]
        keys = [p["key"] for p in presets]
        assert "aider" in keys
        assert "opencode" in keys
        assert "claude" in keys

        # 2. Export Aider preset
        export_resp = client.post(
            "/api/v1/session/presets/export",
            headers={"Authorization": f"Bearer {test_token}"},
            json={"preset_key": "aider", "overwrite": False},
        )
        assert export_resp.status_code == 200
        assert export_resp.json()["status"] == "exported"

        exported_file = root / ".aider.conf.yml"
        assert exported_file.exists()
        assert "openai-api-base: http://127.0.0.1:8000/v1" in exported_file.read_text(encoding="utf-8")

        # 3. Export again without overwrite -> 409 Conflict
        conflict_resp = client.post(
            "/api/v1/session/presets/export",
            headers={"Authorization": f"Bearer {test_token}"},
            json={"preset_key": "aider", "overwrite": False},
        )
        assert conflict_resp.status_code == 409

        # 4. Export with overwrite -> 200 OK
        overwrite_resp = client.post(
            "/api/v1/session/presets/export",
            headers={"Authorization": f"Bearer {test_token}"},
            json={"preset_key": "aider", "overwrite": True},
        )
        assert overwrite_resp.status_code == 200
