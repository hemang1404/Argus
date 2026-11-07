"""WP-3 regression tests: permissions, symlink safety and authorization provenance."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from argus.cli import main
from argus.runtime.candidate import (
    AuthoritativePatchApplier,
    CandidateArtifactStore,
)
from argus.runtime.controller import RecoveryOutcome


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _save(tmp_path: Path, target: Path, patched: str, run_id: str = "run_safety") -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text("def calculate(a, b):\n    return a - b\n", encoding="utf-8")
    CandidateArtifactStore.save_run_artifact(
        run_id=run_id,
        target_file=str(target.relative_to(tmp_path)).replace(os.sep, "/"),
        test_command="pytest",
        baseline_hash=_hash_file(target),
        status="SUCCESS",
        verified=True,
        tier_used=1,
        attempts=1,
        cost_usd=0.0,
        duration_ms=1.0,
        patched_content=patched,
        unified_diff=None,
        workspace_root=tmp_path,
    )
    return run_id


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
@pytest.mark.parametrize("mode", [0o755, 0o600, 0o640])
def test_apply_preserves_permission_mode(tmp_path: Path, mode: int) -> None:
    target = tmp_path / "src" / "calc.py"
    target.parent.mkdir(parents=True)
    target.write_text("def calculate(a, b):\n    return a - b\n", encoding="utf-8")
    os.chmod(target, mode)
    run_id = _save(tmp_path, target, "def calculate(a, b):\n    return a + b\n")

    result = AuthoritativePatchApplier.apply(run_id=run_id, workspace_root=tmp_path)

    assert result.success is True, result.message
    assert result.mode_preserved is True
    assert stat.S_IMODE(target.stat().st_mode) == mode
    assert result.ownership_preserved in (True, False)


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics")
def test_apply_refuses_symlink_target(tmp_path: Path) -> None:
    real = tmp_path / "real.py"
    real.write_text("value = 1\n", encoding="utf-8")
    link = tmp_path / "link.py"
    link.symlink_to(real)

    run_id = _save(tmp_path, link, "value = 2\n")

    result = AuthoritativePatchApplier.apply(run_id=run_id, workspace_root=tmp_path)

    assert result.success is False
    assert result.error_code == "UNSAFE_TARGET_TYPE"
    assert real.read_text(encoding="utf-8") == "value = 1\n"


@pytest.mark.skipif(os.name != "posix", reason="ownership semantics")
def test_ownership_failure_is_reported_truthfully(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "calc.py"
    target.write_text("x = 1\n", encoding="utf-8")
    run_id = _save(tmp_path, target, "x = 2\n")

    def fail_chown(path, uid, gid):
        raise OSError("chown denied")

    monkeypatch.setattr("argus.runtime.candidate.os.chown", fail_chown)
    result = AuthoritativePatchApplier.apply(run_id=run_id, workspace_root=tmp_path)

    assert result.success is True
    assert result.mode_preserved is True
    assert result.ownership_preserved is False
    assert target.read_text(encoding="utf-8") == "x = 2\n"


def test_permission_change_failure_preserves_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "calc.py"
    target.write_text("x = 1\n", encoding="utf-8")
    run_id = _save(tmp_path, target, "x = 2\n")

    def fail_chmod(path, mode):
        raise OSError("chmod denied")

    monkeypatch.setattr("argus.runtime.candidate.os.chmod", fail_chmod)
    result = AuthoritativePatchApplier.apply(run_id=run_id, workspace_root=tmp_path)

    assert result.success is False
    assert result.error_code == "PERMISSION_PRESERVATION_FAILED"
    assert target.read_text(encoding="utf-8") == "x = 1\n"
    assert not list(tmp_path.glob(".argus_tmp_*"))


def test_approval_provenance_is_recorded(tmp_path: Path) -> None:
    target = tmp_path / "calc.py"
    run_id = _save(tmp_path, target, "x = 2\n")

    updated = CandidateArtifactStore.set_approval(
        run_id,
        approved=True,
        workspace_root=tmp_path,
        approved_by="interactive:cli",
        approval_source="cli-approve-interactive",
    )

    assert updated.approved is True
    assert updated.approved_by == "interactive:cli"
    assert updated.approval_source == "cli-approve-interactive"
    assert updated.approval_timestamp == updated.approved_at
    meta = json.loads((tmp_path / ".argus" / "runs" / run_id / "metadata.json").read_text(encoding="utf-8"))
    assert meta["approved_by"] == "interactive:cli"
    assert meta["approval_source"] == "cli-approve-interactive"

    revoked = CandidateArtifactStore.set_approval(run_id, approved=False, workspace_root=tmp_path)
    assert revoked.approved is False
    assert revoked.approved_by is None
    assert revoked.approval_source is None


def test_force_cannot_bypass_candidate_integrity(tmp_path: Path) -> None:
    target = tmp_path / "calc.py"
    run_id = _save(tmp_path, target, "x = 2\n")
    (tmp_path / ".argus" / "runs" / run_id / "candidate.patched").write_text("x = 999\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["apply", "--run-id", run_id, "--workspace", str(tmp_path), "--force"],
    )

    assert result.exit_code == 1
    assert "CONTENT_HASH_MISMATCH" in result.output
    assert target.read_text(encoding="utf-8") == "def calculate(a, b):\n    return a - b\n"


class _FakeController:
    """Returns a verified outcome without any provider or subprocess activity."""

    outcome = None

    def __init__(self, config=None) -> None:
        self.config = config

    def run(self, **kwargs):
        return self.outcome


def test_run_apply_requires_explicit_review_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "calc.py"
    baseline = "def calculate(a, b):\n    return a - b\n"
    target.write_text(baseline, encoding="utf-8")
    patched = "def calculate(a, b):\n    return a + b\n"
    run_id = _save(tmp_path, target, patched, run_id="run_gate")

    _FakeController.outcome = RecoveryOutcome(
        status="SUCCESS",
        total_attempts=1,
        total_cost_usd=0.0,
        total_duration_ms=1.0,
        unified_diff="--- a/calc.py\n+++ b/calc.py\n",
        run_id=run_id,
        target_file="calc.py",
        patched_content=patched,
        baseline_hash=_hash_file(target),
    )
    monkeypatch.setattr("argus.cli.RecoveryController", _FakeController)

    args = [
        "run",
        "--file",
        str(target),
        "--test",
        "pytest",
        "--provider",
        "antigravity",
        "--workspace",
        str(tmp_path),
        "--apply",
    ]
    blocked = CliRunner().invoke(main, args)
    assert blocked.exit_code == 0
    assert "Refusing to apply" in blocked.output
    assert target.read_text(encoding="utf-8") == baseline

    allowed = CliRunner().invoke(main, args + ["--yes-reviewed"])
    assert allowed.exit_code == 0, allowed.output
    assert target.read_text(encoding="utf-8") == patched
    meta = json.loads((tmp_path / ".argus" / "runs" / run_id / "metadata.json").read_text(encoding="utf-8"))
    assert meta["approval_source"] == "cli-run-apply"
    assert meta["approved_by"] == "automation:--yes-reviewed"
    assert result_is_not_human(meta["approved_by"])


def result_is_not_human(approved_by: str) -> bool:
    return approved_by.startswith("automation:")


def test_tui_refuses_candidates_without_authoritative_artifact(tmp_path: Path) -> None:
    """The TUI must not fall back to a direct write when no run artifact exists."""
    from argus.tui.screens.diff_review import DiffReviewScreen

    target = tmp_path / "calc.py"
    target.write_text("x = 1\n", encoding="utf-8")
    screen = DiffReviewScreen(
        workspace=str(tmp_path),
        target_file=str(target),
        diff_text="--- a/calc.py\n+++ b/calc.py\n",
        patched_content="x = 2\n",
        verified=True,
        run_id=None,
    )
    reports: list[str] = []
    screen._report = reports.append  # type: ignore[method-assign]

    screen.action_approve_and_apply()

    assert target.read_text(encoding="utf-8") == "x = 1\n"
    assert reports and "no stored run artifact" in reports[0]
    assert not hasattr(screen, "_apply_direct")
