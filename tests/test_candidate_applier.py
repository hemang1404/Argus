"""Focused regression and contract tests for Candidate Propagation and Authoritative Patch Application.

Covers Stage A Architectural Requirements:
1. Valid candidate applied successfully with metadata update.
2. Missing candidate rejected (CANDIDATE_NOT_FOUND).
3. Candidate that failed validation rejected (CANDIDATE_NOT_VERIFIED).
4. Candidate content tampering/mismatch rejected (CONTENT_HASH_MISMATCH).
5. Stale target file rejected without silent overwrite (STALE_TARGET_CONFLICT).
6. Target outside permitted workspace rejected (PATH_TRAVERSAL_REJECTED).
7. Target file missing on disk rejected (TARGET_NOT_FOUND).
8. Target file mismatch with caller assertion rejected (TARGET_FILE_MISMATCH).
9. Candidate Python syntax error rejected via AST validation (SYNTAX_VALIDATION_FAILED).
10. Concurrent modification during preparation detected and aborted (CONCURRENT_MODIFICATION_DETECTED).
11. Original file remains intact and tempfile cleaned up when I/O replacement fails.
12. Multi-run artifact isolation in `.argus/runs/<run_id>/`.
13. CLI `argus apply` command integration.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch
import pytest
from click.testing import CliRunner

from argus.cli import main
from argus.runtime.candidate import (
    AuthoritativePatchApplier,
    CandidateArtifactStore,
    CandidateMetadata,
)


def _hash_file(file_path: Path) -> str:
    """Compute exact SHA-256 hash of file bytes on disk (cross-platform CRLF/LF safe)."""
    return hashlib.sha256(file_path.read_bytes()).hexdigest()


def _hash_str(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_valid_candidate_applied_successfully(tmp_path: Path):
    """Verify that a verified candidate patch is atomically applied to the target source file."""
    target_file = tmp_path / "src" / "calc.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    baseline_code = "def calculate(a, b):\n    return a - b\n"
    target_file.write_text(baseline_code, encoding="utf-8")
    baseline_hash = _hash_file(target_file)

    patched_code = "def calculate(a, b):\n    return a + b\n"
    run_id = "run_20261022_001_success"

    # Save run artifact
    run_dir = CandidateArtifactStore.save_run_artifact(
        run_id=run_id,
        target_file="src/calc.py",
        test_command="pytest",
        baseline_hash=baseline_hash,
        status="SUCCESS",
        verified=True,
        tier_used=1,
        attempts=1,
        cost_usd=0.0001,
        duration_ms=45.0,
        patched_content=patched_code,
        unified_diff="--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1,2 +1,2 @@\n-    return a - b\n+    return a + b\n",
        workspace_root=tmp_path,
    )
    assert run_dir.is_dir()

    # Apply patch
    result = AuthoritativePatchApplier.apply(run_id=run_id, workspace_root=tmp_path)

    assert result.success is True
    assert result.error_code is None
    assert result.applied_at is not None
    assert "calc.py" in result.message

    # Target on disk must have the new code
    assert target_file.read_text(encoding="utf-8") == patched_code

    # Metadata record must be marked as applied
    meta_path = run_dir / "metadata.json"
    meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta_data["applied"] is True
    assert meta_data["applied_at"] == result.applied_at


def test_missing_candidate_rejected(tmp_path: Path):
    """Verify that attempting to apply a non-existent candidate fails safely."""
    result = AuthoritativePatchApplier.apply(run_id="run_does_not_exist", workspace_root=tmp_path)
    assert result.success is False
    assert result.error_code == "CANDIDATE_NOT_FOUND"


def test_candidate_not_verified_rejected(tmp_path: Path):
    """Verify that an unverified candidate (e.g. failed attempts) cannot be applied."""
    target_file = tmp_path / "mod.py"
    orig_code = "x = 10\n"
    target_file.write_text(orig_code, encoding="utf-8")

    run_id = "run_failed_attempts"
    CandidateArtifactStore.save_run_artifact(
        run_id=run_id,
        target_file="mod.py",
        test_command="pytest",
        baseline_hash=_hash_file(target_file),
        status="FAILED_MAX_ATTEMPTS",
        verified=False,
        tier_used=None,
        attempts=4,
        cost_usd=0.005,
        duration_ms=1200.0,
        patched_content="x = 20\n",
        unified_diff=None,
        workspace_root=tmp_path,
    )

    result = AuthoritativePatchApplier.apply(run_id=run_id, workspace_root=tmp_path)
    assert result.success is False
    assert result.error_code == "CANDIDATE_NOT_VERIFIED"
    # Original file must remain untouched
    assert target_file.read_text(encoding="utf-8") == orig_code


def test_candidate_tampered_hash_mismatch_rejected(tmp_path: Path):
    """Verify that if candidate code does not match its cryptographic hash, application is rejected."""
    target_file = tmp_path / "core.py"
    orig_code = "print('hello')\n"
    target_file.write_text(orig_code, encoding="utf-8")

    run_id = "run_tampered_test"
    run_dir = CandidateArtifactStore.save_run_artifact(
        run_id=run_id,
        target_file="core.py",
        test_command="pytest",
        baseline_hash=_hash_file(target_file),
        status="SUCCESS",
        verified=True,
        tier_used=1,
        attempts=1,
        cost_usd=0.0001,
        duration_ms=50.0,
        patched_content="print('world')\n",
        unified_diff=None,
        workspace_root=tmp_path,
    )

    # Tamper with the saved candidate file behind the back of metadata
    (run_dir / "candidate.patched").write_text("print('malicious injection')\n", encoding="utf-8")

    result = AuthoritativePatchApplier.apply(run_id=run_id, workspace_root=tmp_path)
    assert result.success is False
    assert result.error_code == "CONTENT_HASH_MISMATCH"
    assert target_file.read_text(encoding="utf-8") == orig_code


def test_stale_target_file_rejected_without_overwrite(tmp_path: Path):
    """Verify that if target file was modified concurrently on disk, application is refused."""
    target_file = tmp_path / "service.py"
    code_v1 = "def serve(): return 'v1'\n"
    target_file.write_text(code_v1, encoding="utf-8")
    baseline_hash_v1 = _hash_file(target_file)

    run_id = "run_stale_check"
    CandidateArtifactStore.save_run_artifact(
        run_id=run_id,
        target_file="service.py",
        test_command="pytest",
        baseline_hash=baseline_hash_v1,
        status="SUCCESS",
        verified=True,
        tier_used=1,
        attempts=1,
        cost_usd=0.0001,
        duration_ms=40.0,
        patched_content="def serve(): return 'fixed_v1'\n",
        unified_diff=None,
        workspace_root=tmp_path,
    )

    # Concurrent developer edit occurs while recovery was running
    code_v2_user = "def serve(): return 'v2_custom_changes'\n"
    target_file.write_text(code_v2_user, encoding="utf-8")

    result = AuthoritativePatchApplier.apply(run_id=run_id, workspace_root=tmp_path)
    assert result.success is False
    assert result.error_code == "STALE_TARGET_CONFLICT"
    # Essential invariant: user changes are NOT silently clobbered
    assert target_file.read_text(encoding="utf-8") == code_v2_user


def test_path_traversal_outside_workspace_rejected(tmp_path: Path):
    """Verify that path traversal attempts escaping the workspace are rejected."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside_file = tmp_path / "secret.py"
    outside_file.write_text("SECRET_KEY = 123\n", encoding="utf-8")

    run_id = "run_traversal_exploit"
    CandidateArtifactStore.save_run_artifact(
        run_id=run_id,
        target_file="../secret.py",
        test_command="pytest",
        baseline_hash=_hash_file(outside_file),
        status="SUCCESS",
        verified=True,
        tier_used=1,
        attempts=1,
        cost_usd=0.0001,
        duration_ms=10.0,
        patched_content="SECRET_KEY = 999\n",
        unified_diff=None,
        workspace_root=ws,
    )

    result = AuthoritativePatchApplier.apply(run_id=run_id, workspace_root=ws)
    assert result.success is False
    assert result.error_code == "PATH_TRAVERSAL_REJECTED"
    assert outside_file.read_text(encoding="utf-8") == "SECRET_KEY = 123\n"


def test_target_file_not_found(tmp_path: Path):
    """Verify that if the target file is missing on disk, application is rejected."""
    run_id = "run_missing_target"
    CandidateArtifactStore.save_run_artifact(
        run_id=run_id,
        target_file="deleted_file.py",
        test_command="pytest",
        baseline_hash="fakehash123",
        status="SUCCESS",
        verified=True,
        tier_used=1,
        attempts=1,
        cost_usd=0.0001,
        duration_ms=10.0,
        patched_content="content\n",
        unified_diff=None,
        workspace_root=tmp_path,
    )

    result = AuthoritativePatchApplier.apply(run_id=run_id, workspace_root=tmp_path)
    assert result.success is False
    assert result.error_code == "TARGET_NOT_FOUND"


def test_target_mismatch_with_caller_assertion_rejected(tmp_path: Path):
    """Verify that if caller asserts a different target than the run produced, it is rejected."""
    f1 = tmp_path / "file1.py"
    f1.write_text("x = 1\n", encoding="utf-8")

    run_id = "run_mismatch_test"
    CandidateArtifactStore.save_run_artifact(
        run_id=run_id,
        target_file="file1.py",
        test_command="pytest",
        baseline_hash=_hash_file(f1),
        status="SUCCESS",
        verified=True,
        tier_used=1,
        attempts=1,
        cost_usd=0.0001,
        duration_ms=10.0,
        patched_content="x = 2\n",
        unified_diff=None,
        workspace_root=tmp_path,
    )

    result = AuthoritativePatchApplier.apply(
        run_id=run_id,
        workspace_root=tmp_path,
        expected_target="file2.py",
    )
    assert result.success is False
    assert result.error_code == "TARGET_FILE_MISMATCH"
    assert f1.read_text(encoding="utf-8") == "x = 1\n"


def test_syntax_validation_failed_rejected(tmp_path: Path):
    """Verify that invalid Python syntax in candidate code is caught by AST validation and rejected."""
    target_file = tmp_path / "syntax_err.py"
    orig_code = "def valid(): pass\n"
    target_file.write_text(orig_code, encoding="utf-8")

    broken_syntax = "def broken(:\n    pass\n"
    run_id = "run_syntax_err"
    CandidateArtifactStore.save_run_artifact(
        run_id=run_id,
        target_file="syntax_err.py",
        test_command="pytest",
        baseline_hash=_hash_file(target_file),
        status="SUCCESS",
        verified=True,
        tier_used=1,
        attempts=1,
        cost_usd=0.0001,
        duration_ms=10.0,
        patched_content=broken_syntax,
        unified_diff=None,
        workspace_root=tmp_path,
    )

    result = AuthoritativePatchApplier.apply(run_id=run_id, workspace_root=tmp_path)
    assert result.success is False
    assert result.error_code == "SYNTAX_VALIDATION_FAILED"
    assert target_file.read_text(encoding="utf-8") == orig_code


def test_concurrent_modification_detected_pre_replace(tmp_path: Path):
    """Verify that if target file changes between initial baseline check and pre-replace check, it is caught."""
    target_file = tmp_path / "race.py"
    orig_code = "val = 100\n"
    target_file.write_text(orig_code, encoding="utf-8")
    actual_baseline_hash = _hash_file(target_file)

    run_id = "run_race_detect"
    CandidateArtifactStore.save_run_artifact(
        run_id=run_id,
        target_file="race.py",
        test_command="pytest",
        baseline_hash=actual_baseline_hash,
        status="SUCCESS",
        verified=True,
        tier_used=1,
        attempts=1,
        cost_usd=0.0001,
        duration_ms=20.0,
        patched_content="val = 200\n",
        unified_diff=None,
        workspace_root=tmp_path,
    )

    # Simulate race condition: first read_bytes gives baseline bytes, second read_bytes gives modified bytes
    real_read_bytes = Path.read_bytes
    call_count = 0

    def mock_read_bytes(self_path):
        nonlocal call_count
        if self_path.name == "race.py":
            call_count += 1
            if call_count == 1:
                return real_read_bytes(self_path)
            else:
                return b"val = 999 (concurrently changed right before replace)"
        return real_read_bytes(self_path)

    with patch.object(Path, "read_bytes", mock_read_bytes):
        result = AuthoritativePatchApplier.apply(run_id=run_id, workspace_root=tmp_path)

    assert result.success is False
    assert result.error_code == "CONCURRENT_MODIFICATION_DETECTED"
    # Ensure any temp files cleaned up
    remaining_tmps = [p for p in target_file.parent.iterdir() if p.name.startswith(".argus_tmp_")]
    assert len(remaining_tmps) == 0


def test_original_file_intact_when_io_fails(tmp_path: Path):
    """Verify that if atomic replace raises OSError, original file remains intact and temp is cleaned up."""
    target_file = tmp_path / "io_test.py"
    orig_code = "stable = True\n"
    target_file.write_text(orig_code, encoding="utf-8")
    baseline_hash = _hash_file(target_file)

    run_id = "run_io_fail"
    CandidateArtifactStore.save_run_artifact(
        run_id=run_id,
        target_file="io_test.py",
        test_command="pytest",
        baseline_hash=baseline_hash,
        status="SUCCESS",
        verified=True,
        tier_used=1,
        attempts=1,
        cost_usd=0.0001,
        duration_ms=15.0,
        patched_content="stable = False\n",
        unified_diff=None,
        workspace_root=tmp_path,
    )

    with patch("os.replace", side_effect=OSError("Disk full or permission denied")):
        result = AuthoritativePatchApplier.apply(run_id=run_id, workspace_root=tmp_path)

    assert result.success is False
    assert result.error_code == "APPLY_IO_ERROR"
    # Invariant: Original file untouched
    assert target_file.read_text(encoding="utf-8") == orig_code
    # Invariant: Temporary file deleted
    remaining_tmps = [p for p in target_file.parent.iterdir() if p.name.startswith(".argus_tmp_")]
    assert len(remaining_tmps) == 0


def test_candidate_artifact_store_isolation_and_last_run(tmp_path: Path):
    """Verify multi-run isolation in .argus/runs/<run_id>/ and last_run_id pointer."""
    # Run 1
    CandidateArtifactStore.save_run_artifact(
        run_id="run_001",
        target_file="f1.py",
        test_command="pytest",
        baseline_hash="hash1",
        status="SUCCESS",
        verified=True,
        tier_used=1,
        attempts=1,
        cost_usd=0.001,
        duration_ms=10.0,
        patched_content="content1",
        unified_diff="diff1",
        workspace_root=tmp_path,
    )
    assert CandidateArtifactStore.get_last_run_id(tmp_path) == "run_001"

    # Run 2
    CandidateArtifactStore.save_run_artifact(
        run_id="run_002",
        target_file="f2.py",
        test_command="pytest",
        baseline_hash="hash2",
        status="FAILED_MAX_ATTEMPTS",
        verified=False,
        tier_used=None,
        attempts=4,
        cost_usd=0.004,
        duration_ms=50.0,
        patched_content=None,
        unified_diff=None,
        workspace_root=tmp_path,
    )
    assert CandidateArtifactStore.get_last_run_id(tmp_path) == "run_002"

    # Verify both exist independently
    meta1, patch1 = CandidateArtifactStore.load_candidate("run_001", workspace_root=tmp_path)
    assert meta1.run_id == "run_001"
    assert patch1 == "content1"

    meta2, patch2 = CandidateArtifactStore.load_candidate("run_002", workspace_root=tmp_path)
    assert meta2.run_id == "run_002"
    assert patch2 is None


def test_cli_apply_command_integration(tmp_path: Path):
    """Verify CLI command `argus apply` integration with real workspace."""
    target_file = tmp_path / "hello.py"
    target_file.write_text("print('before')\n", encoding="utf-8")
    baseline_hash = _hash_file(target_file)

    run_id = "run_cli_test"
    CandidateArtifactStore.save_run_artifact(
        run_id=run_id,
        target_file="hello.py",
        test_command="pytest",
        baseline_hash=baseline_hash,
        status="SUCCESS",
        verified=True,
        tier_used=1,
        attempts=1,
        cost_usd=0.0001,
        duration_ms=15.0,
        patched_content="print('after')\n",
        unified_diff=None,
        workspace_root=tmp_path,
    )

    runner = CliRunner()

    # 1. Applying unapproved candidate fails with CANDIDATE_NOT_APPROVED
    res_unapproved = runner.invoke(main, ["apply", "--workspace", str(tmp_path)])
    assert res_unapproved.exit_code == 1
    assert "CANDIDATE_NOT_APPROVED" in res_unapproved.output

    # 2. Human approval via CLI
    res_approve = runner.invoke(main, ["approve", run_id, "--workspace", str(tmp_path), "-y"])
    assert res_approve.exit_code == 0
    assert "APPROVED" in res_approve.output

    # 3. Success case using last_run_id after approval
    res = runner.invoke(main, ["apply", "--workspace", str(tmp_path)])
    assert res.exit_code == 0
    assert "APPLIED" in res.output
    assert target_file.read_text(encoding="utf-8") == "print('after')\n"

    # 4. Failure case: applying again should fail with STALE_TARGET_CONFLICT because content changed to 'after'
    res_stale = runner.invoke(main, ["apply", "--run-id", run_id, "--workspace", str(tmp_path)])
    assert res_stale.exit_code == 1
    assert "STALE_TARGET_CONFLICT" in res_stale.output
