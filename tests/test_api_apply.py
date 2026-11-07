"""Tests for Phase 4: Diff Review, Verification & Safe In-Place Patch Application."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from argus.api.recovery import RecoveryTaskRequest, calculate_file_hash, task_manager
from argus.api.server import create_app
from argus.runtime.candidate import ApplyResult, CandidateArtifactStore
from argus.runtime.controller import RecoveryOutcome


def test_apply_verified_patch_success():
    """Verify that an approved patch applies cleanly and updates the file and .argus/last_run.diff."""
    with tempfile.TemporaryDirectory() as tmp_root:
        root = Path(tmp_root).resolve()
        target = root / "src" / "math_util.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        original_code = "def multiply(x, y): return x + y\n"
        target.write_text(original_code, encoding="utf-8")

        test_token = "auth-token-apply"
        app = create_app(workspace=str(root), token=test_token)
        client = TestClient(app)

        # Create a mock completed task state
        req = RecoveryTaskRequest(
            target_file="src/math_util.py",
            test_command="pytest",
        )
        state = task_manager.create_task(req, root)
        state.baseline_hash = calculate_file_hash(target)

        fixed_code = "def multiply(x, y): return x * y\n"
        fake_diff = "--- a/src/math_util.py\n+++ b/src/math_util.py\n@@ -1 +1 @@\n-def multiply(x, y): return x + y\n+def multiply(x, y): return x * y\n"
        state.outcome = RecoveryOutcome(
            status="SUCCESS",
            total_attempts=1,
            total_cost_usd=0.0001,
            total_duration_ms=120.0,
            unified_diff=fake_diff,
            patched_content=fixed_code,
            target_file=str(target),
        )

        # 1. Retrieve diff metadata
        diff_resp = client.get(
            f"/api/v1/recovery/diff/{state.task_id}",
            headers={"Authorization": f"Bearer {test_token}"},
        )
        assert diff_resp.status_code == 200
        assert diff_resp.json()["status"] == "SUCCESS"
        assert "multiply(x, y): return x * y" in diff_resp.json()["unified_diff"]

        # 2. Apply patch
        apply_resp = client.post(
            "/api/v1/recovery/apply",
            headers={"Authorization": f"Bearer {test_token}"},
            json={
                "task_id": state.task_id,
                "target_file": "src/math_util.py",
            },
        )
        assert apply_resp.status_code == 200
        assert apply_resp.json()["status"] == "applied"

        # Verify file content on disk is updated
        assert target.read_text(encoding="utf-8") == fixed_code

        # Verify .argus/last_run.diff was written
        diff_file = root / ".argus" / "last_run.diff"
        assert diff_file.exists()
        assert fake_diff in diff_file.read_text(encoding="utf-8")

        # An artifact-less outcome is materialized and applied through the
        # authoritative applier, not by a direct write.
        assert state.outcome.run_id
        meta = json.loads(
            (root / ".argus" / "runs" / state.outcome.run_id / "metadata.json").read_text(encoding="utf-8")
        )
        assert meta["approved_by"] == "interactive:api"
        assert meta["applied"] is True


def test_apply_patch_concurrency_refusal():
    """Verify that modifying target file concurrently triggers HTTP 409 Conflict."""
    with tempfile.TemporaryDirectory() as tmp_root:
        root = Path(tmp_root).resolve()
        target = root / "src" / "math_util.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("initial code", encoding="utf-8")

        test_token = "auth-token-concurrency"
        app = create_app(workspace=str(root), token=test_token)
        client = TestClient(app)

        req = RecoveryTaskRequest(target_file="src/math_util.py", test_command="pytest")
        state = task_manager.create_task(req, root)
        state.baseline_hash = calculate_file_hash(target)
        state.outcome = RecoveryOutcome(
            status="SUCCESS",
            total_attempts=1,
            total_cost_usd=0.0001,
            total_duration_ms=100.0,
            patched_content="fixed code",
        )

        # Concurrently modify file on disk before applying patch
        target.write_text("concurrent manual developer edit", encoding="utf-8")

        resp = client.post(
            "/api/v1/recovery/apply",
            headers={"Authorization": f"Bearer {test_token}"},
            json={"task_id": state.task_id, "target_file": "src/math_util.py"},
        )
        assert resp.status_code == 409
        assert "modified on disk concurrently" in resp.json()["detail"]


def test_apply_patch_unverified_task_rejection():
    """Verify that applying an unsuccessful or missing task outcome is rejected with HTTP 400 or 404."""
    with tempfile.TemporaryDirectory() as tmp_root:
        root = Path(tmp_root).resolve()
        target = root / "file.py"
        target.write_text("x = 1", encoding="utf-8")

        test_token = "auth-token-unverified"
        app = create_app(workspace=str(root), token=test_token)
        client = TestClient(app)

        # 1. Non-existent task -> 404
        resp = client.post(
            "/api/v1/recovery/apply",
            headers={"Authorization": f"Bearer {test_token}"},
            json={"task_id": "non-existent-id", "target_file": "file.py"},
        )
        assert resp.status_code == 404

        # 2. Failed task status -> 400
        req = RecoveryTaskRequest(target_file="file.py", test_command="pytest")
        state = task_manager.create_task(req, root)
        state.outcome = RecoveryOutcome(
            status="FAILED_MAX_ATTEMPTS",
            total_attempts=4,
            total_cost_usd=0.02,
            total_duration_ms=400.0,
        )
        resp2 = client.post(
            "/api/v1/recovery/apply",
            headers={"Authorization": f"Bearer {test_token}"},
            json={"task_id": state.task_id, "target_file": "file.py"},
        )
        assert resp2.status_code == 400
        assert "Cannot apply unverified patch" in resp2.json()["detail"]


def _save_api_artifact(root: Path, target: Path, patched: str, run_id: str = "run_api_backed") -> str:
    CandidateArtifactStore.save_run_artifact(
        run_id=run_id,
        target_file=target.relative_to(root).as_posix(),
        test_command="pytest",
        baseline_hash=calculate_file_hash(target),
        status="SUCCESS",
        verified=True,
        tier_used=1,
        attempts=1,
        cost_usd=0.0,
        duration_ms=1.0,
        patched_content=patched,
        unified_diff=None,
        workspace_root=root,
    )
    return run_id


def test_apply_artifact_backed_outcome_uses_authoritative_applier():
    with tempfile.TemporaryDirectory() as tmp_root:
        root = Path(tmp_root).resolve()
        target = root / "src" / "math_util.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def multiply(x, y): return x + y\n", encoding="utf-8")

        fixed = "def multiply(x, y): return x * y\n"
        run_id = _save_api_artifact(root, target, fixed)
        state = task_manager.create_task(
            RecoveryTaskRequest(target_file="src/math_util.py", test_command="pytest"),
            root,
        )
        state.baseline_hash = calculate_file_hash(target)
        state.outcome = RecoveryOutcome(
            status="SUCCESS",
            total_attempts=1,
            total_cost_usd=0.0,
            total_duration_ms=1.0,
            patched_content=fixed,
            target_file=str(target),
            run_id=run_id,
        )

        app = create_app(workspace=str(root), token="auth-token-backend")
        client = TestClient(app)
        resp = client.post(
            "/api/v1/recovery/apply",
            headers={"Authorization": "Bearer auth-token-backend"},
            json={"task_id": state.task_id, "target_file": "src/math_util.py"},
        )

        assert resp.status_code == 200, resp.text
        assert target.read_text(encoding="utf-8") == fixed


def test_apply_artifact_backed_tamper_is_refused_without_write():
    with tempfile.TemporaryDirectory() as tmp_root:
        root = Path(tmp_root).resolve()
        target = root / "src" / "math_util.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        original = "def multiply(x, y): return x + y\n"
        target.write_text(original, encoding="utf-8")

        fixed = "def multiply(x, y): return x * y\n"
        run_id = _save_api_artifact(root, target, fixed, run_id="run_api_tampered")
        # Tamper with the persisted candidate after metadata/hash creation.
        (root / ".argus" / "runs" / run_id / "candidate.patched").write_text(
            "def multiply(x, y): return 999\n", encoding="utf-8"
        )

        state = task_manager.create_task(
            RecoveryTaskRequest(target_file="src/math_util.py", test_command="pytest"),
            root,
        )
        state.baseline_hash = calculate_file_hash(target)
        state.outcome = RecoveryOutcome(
            status="SUCCESS",
            total_attempts=1,
            total_cost_usd=0.0,
            total_duration_ms=1.0,
            patched_content=fixed,
            target_file=str(target),
            run_id=run_id,
        )

        app = create_app(workspace=str(root), token="auth-token-tampered")
        client = TestClient(app)
        resp = client.post(
            "/api/v1/recovery/apply",
            headers={"Authorization": "Bearer auth-token-tampered"},
            json={"task_id": state.task_id, "target_file": "src/math_util.py"},
        )

        assert resp.status_code == 400
        assert "hash mismatch" in resp.json()["detail"].lower()
        assert target.read_text(encoding="utf-8") == original


def test_artifactless_outcome_without_baseline_refuses_after_external_edit():
    with tempfile.TemporaryDirectory() as tmp_root:
        root = Path(tmp_root).resolve()
        target = root / "calc.py"
        target.write_text("x = 1\n", encoding="utf-8")

        state = task_manager.create_task(
            RecoveryTaskRequest(target_file="calc.py", test_command="pytest"),
            root,
        )
        # Deliberately omit both recorded baselines. The candidate was generated
        # against "x = 1\n"; an external edit now exists on disk.
        state.baseline_hash = ""
        state.outcome = RecoveryOutcome(
            status="SUCCESS",
            total_attempts=1,
            total_cost_usd=0.0,
            total_duration_ms=1.0,
            patched_content="x = 2\n",
            target_file=str(target),
            baseline_hash="",
        )
        external = "x = 999\n"
        target.write_text(external, encoding="utf-8")

        app = create_app(workspace=str(root), token="auth-token-no-baseline")
        client = TestClient(app)
        resp = client.post(
            "/api/v1/recovery/apply",
            headers={"Authorization": "Bearer auth-token-no-baseline"},
            json={"task_id": state.task_id, "target_file": "calc.py"},
        )

        assert resp.status_code == 409
        assert "BASELINE_UNAVAILABLE" in resp.json()["detail"]
        assert target.read_text(encoding="utf-8") == external


def test_artifactless_outcome_cannot_bypass_applier(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_root:
        root = Path(tmp_root).resolve()
        target = root / "calc.py"
        original = "x = 1\n"
        target.write_text(original, encoding="utf-8")

        state = task_manager.create_task(
            RecoveryTaskRequest(target_file="calc.py", test_command="pytest"),
            root,
        )
        state.baseline_hash = calculate_file_hash(target)
        state.outcome = RecoveryOutcome(
            status="SUCCESS",
            total_attempts=1,
            total_cost_usd=0.0,
            total_duration_ms=1.0,
            patched_content="x = 2\n",
            target_file=str(target),
        )

        # Force the authoritative applier to fail. If the endpoint had a direct
        # write fallback, the file would still change.
        monkeypatch.setattr(
            "argus.api.recovery.AuthoritativePatchApplier.apply",
            staticmethod(
                lambda **kwargs: ApplyResult(
                    success=False,
                    run_id="forced",
                    target_file="calc.py",
                    error_code="CANDIDATE_NOT_VERIFIED",
                    message="forced applier failure",
                )
            ),
        )

        app = create_app(workspace=str(root), token="auth-token-bypass")
        client = TestClient(app)
        resp = client.post(
            "/api/v1/recovery/apply",
            headers={"Authorization": "Bearer auth-token-bypass"},
            json={"task_id": state.task_id, "target_file": "calc.py"},
        )

        assert resp.status_code == 400
        assert target.read_text(encoding="utf-8") == original
        # The endpoint materialized a real artifact before invoking the applier.
        assert state.outcome.run_id
        assert (root / ".argus" / "runs" / state.outcome.run_id / "metadata.json").is_file()
