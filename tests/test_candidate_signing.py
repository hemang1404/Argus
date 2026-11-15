"""Approval-gate integrity: signed run metadata, strict run ids, key outside the workspace."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from argus.runtime.candidate import (
    SIGNATURE_FILENAME,
    SIGNING_KEY_FILENAME,
    AuthoritativePatchApplier,
    CandidateArtifactStore,
    InvalidRunIdError,
    validate_run_id,
)

BASELINE = "def f():\n    return 1\n"
PATCHED = "def f():\n    return 2\n"
EVIL = "print('pwned')\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_run(ws: Path, run_id: str = "run_20260101_000000_abcd1234") -> Path:
    (ws / "m.py").write_bytes(BASELINE.encode("utf-8"))
    return CandidateArtifactStore.save_run_artifact(
        run_id=run_id,
        target_file="m.py",
        test_command="pytest",
        baseline_hash=_sha(BASELINE),
        status="SUCCESS",
        verified=True,
        tier_used=1,
        attempts=1,
        cost_usd=0.0,
        duration_ms=1.0,
        patched_content=PATCHED,
        unified_diff="",
        workspace_root=ws,
    )


def test_signing_key_lives_outside_workspace(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("ARGUS_CONFIG_DIR", str(cfg))
    _make_run(ws)
    assert (cfg / SIGNING_KEY_FILENAME).is_file()
    assert not list(ws.rglob(SIGNING_KEY_FILENAME))


def test_config_dir_inside_workspace_is_refused(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("ARGUS_CONFIG_DIR", str(ws / ".cfg"))
    with pytest.raises(Exception) as exc:
        _make_run(ws)
    assert "workspace" in str(exc.value)


def test_forged_metadata_cannot_be_applied(tmp_path):
    """A hand-written metadata.json (verified+approved, matching hashes) is rejected."""
    ws = tmp_path
    (ws / "m.py").write_bytes(BASELINE.encode("utf-8"))
    run_dir = ws / ".argus" / "runs" / "evil_run"
    run_dir.mkdir(parents=True)
    (run_dir / "candidate.patched").write_text(EVIL, encoding="utf-8")
    forged = {
        "run_id": "evil_run", "target_file": "m.py", "test_command": "x",
        "baseline_hash": _sha(BASELINE), "candidate_hash": _sha(EVIL),
        "status": "SUCCESS", "verified": True, "tier_used": 1, "attempts": 1,
        "cost_usd": 0.0, "duration_ms": 1.0, "created_at": "2026-01-01T00:00:00+00:00",
        "approved": True, "approved_by": "interactive:cli", "approval_source": "cli-approve",
    }
    (run_dir / "metadata.json").write_text(json.dumps(forged), encoding="utf-8")
    (ws / ".argus" / "last_run_id").write_text("evil_run", encoding="utf-8")

    for sig in (None, "0" * 64):
        if sig:
            (run_dir / SIGNATURE_FILENAME).write_text(sig, encoding="utf-8")
        res = AuthoritativePatchApplier.apply("evil_run", workspace_root=ws, require_approval=True)
        assert not res.success
        assert res.error_code == "CANDIDATE_SIGNATURE_INVALID"
        assert (ws / "m.py").read_bytes() == BASELINE.encode("utf-8")
    assert CandidateArtifactStore.list_candidates(ws) == []


def test_editing_signed_metadata_invalidates_it(tmp_path):
    run_dir = _make_run(tmp_path)
    meta_path = run_dir / "metadata.json"
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    data["approved"] = True
    meta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    res = AuthoritativePatchApplier.apply(run_dir.name, workspace_root=tmp_path, require_approval=True)
    assert res.error_code == "CANDIDATE_SIGNATURE_INVALID"


def test_signed_run_approve_apply_roundtrip(tmp_path):
    run_dir = _make_run(tmp_path)
    run_id = run_dir.name
    unapproved = AuthoritativePatchApplier.apply(run_id, workspace_root=tmp_path, require_approval=True)
    assert unapproved.error_code == "CANDIDATE_NOT_APPROVED"
    CandidateArtifactStore.set_approval(run_id, True, workspace_root=tmp_path, approved_by="t", approval_source="t")
    res = AuthoritativePatchApplier.apply(run_id, workspace_root=tmp_path, require_approval=True)
    assert res.success, res.message
    assert (tmp_path / "m.py").read_bytes() == PATCHED.encode("utf-8")
    # metadata is re-signed after apply, so it still loads
    meta, _ = CandidateArtifactStore.load_candidate(run_id, workspace_root=tmp_path)
    assert meta.applied


def test_candidate_file_tamper_after_signing_is_rejected(tmp_path):
    run_dir = _make_run(tmp_path)
    (run_dir / "candidate.patched").write_text(EVIL, encoding="utf-8")
    CandidateArtifactStore.set_approval(run_dir.name, True, workspace_root=tmp_path)
    res = AuthoritativePatchApplier.apply(run_dir.name, workspace_root=tmp_path, require_approval=True)
    assert res.error_code == "CONTENT_HASH_MISMATCH"


def test_metadata_copied_from_another_run_dir_is_rejected(tmp_path):
    a = _make_run(tmp_path, "run_a")
    b = tmp_path / ".argus" / "runs" / "run_b"
    b.mkdir()
    for name in ("metadata.json", SIGNATURE_FILENAME, "candidate.patched"):
        (b / name).write_bytes((a / name).read_bytes())
    res = AuthoritativePatchApplier.apply("run_b", workspace_root=tmp_path)
    assert res.error_code == "CANDIDATE_SIGNATURE_INVALID"


@pytest.mark.parametrize("bad", ["../../evil", "..", ".", "a/b", "a\\b", "", "/abs", "x" * 200, "run id", "..\\..\\x"])
def test_invalid_run_ids_are_rejected(tmp_path, bad):
    with pytest.raises(InvalidRunIdError):
        validate_run_id(bad)
    res = AuthoritativePatchApplier.apply(bad, workspace_root=tmp_path)
    assert not res.success
    assert res.error_code == "INVALID_RUN_ID"


def test_run_id_traversal_does_not_reach_outside_runs_dir(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "evil"
    outside.mkdir()
    (outside / "metadata.json").write_text("{}", encoding="utf-8")
    res = AuthoritativePatchApplier.apply("../../evil", workspace_root=ws)
    assert res.error_code == "INVALID_RUN_ID"


def test_last_run_id_pointer_is_validated(tmp_path):
    (tmp_path / ".argus").mkdir()
    (tmp_path / ".argus" / "last_run_id").write_text("../../evil", encoding="utf-8")
    assert CandidateArtifactStore.get_last_run_id(tmp_path) is None
    (tmp_path / ".argus" / "last_run_id").write_text("run_ok-1", encoding="utf-8")
    assert CandidateArtifactStore.get_last_run_id(tmp_path) == "run_ok-1"


def test_save_rejects_traversal_run_id(tmp_path):
    with pytest.raises(InvalidRunIdError):
        CandidateArtifactStore.save_run_artifact(
            run_id="../x", target_file="m.py", test_command="t", baseline_hash="h", status="SUCCESS",
            verified=True, tier_used=1, attempts=1, cost_usd=0.0, duration_ms=1.0,
            patched_content="x", unified_diff="", workspace_root=tmp_path,
        )
