"""Authoritative Candidate Artifact Management and Atomic Patch Application.

Implements Stage A Architectural Requirements:
1. Per-run artifact isolation in `.argus/runs/<run_id>/` preventing concurrency collisions.
2. Cryptographic baseline integrity and candidate content hash assertion.
3. Universal AST syntax validation on candidate code before application.
4. Path traversal prevention (workspace boundary containment).
5. Atomic replacement on the same filesystem using temporary files and flush/fsync.
6. Detection of concurrent file modifications immediately prior to directory replacement.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from argus.patch.ast_checker import ASTChecker
from argus.utils.redaction import scrub_object, scrub_secrets


class CandidateApplicationError(Exception):
    """Base exception for candidate patch application failures."""
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class CandidateNotFoundError(CandidateApplicationError):
    def __init__(self, message: str = "Candidate run artifact not found.") -> None:
        super().__init__("CANDIDATE_NOT_FOUND", message)


class CandidateNotVerifiedError(CandidateApplicationError):
    def __init__(self, message: str = "Candidate is not marked as verified passing.") -> None:
        super().__init__("CANDIDATE_NOT_VERIFIED", message)


class CandidateNotApprovedError(CandidateApplicationError):
    def __init__(self, message: str = "Candidate has not received explicit human approval.") -> None:
        super().__init__("CANDIDATE_NOT_APPROVED", message)


class TargetNotFoundError(CandidateApplicationError):
    def __init__(self, message: str = "Target file does not exist on disk.") -> None:
        super().__init__("TARGET_NOT_FOUND", message)


class TargetMismatchError(CandidateApplicationError):
    def __init__(self, message: str = "Target file does not match candidate metadata target.") -> None:
        super().__init__("TARGET_FILE_MISMATCH", message)


class PathTraversalError(CandidateApplicationError):
    def __init__(self, message: str = "Target path escapes the permitted workspace root.") -> None:
        super().__init__("PATH_TRAVERSAL_REJECTED", message)


class CandidateTamperedError(CandidateApplicationError):
    def __init__(self, message: str = "Candidate patched content does not match recorded SHA-256 hash.") -> None:
        super().__init__("CONTENT_HASH_MISMATCH", message)


class StaleTargetError(CandidateApplicationError):
    def __init__(self, message: str = "Target file has changed on disk since baseline was captured.") -> None:
        super().__init__("STALE_TARGET_CONFLICT", message)


class ConcurrentModificationError(CandidateApplicationError):
    def __init__(self, message: str = "Target file was modified concurrently while preparing patch application.") -> None:
        super().__init__("CONCURRENT_MODIFICATION_DETECTED", message)


class SyntaxValidationError(CandidateApplicationError):
    def __init__(self, message: str = "Candidate code contains Python syntax errors.") -> None:
        super().__init__("SYNTAX_VALIDATION_FAILED", message)


@dataclass
class CandidateMetadata:
    """Immutable metadata record for a recovery run's candidate patch."""
    run_id: str
    target_file: str
    test_command: str
    baseline_hash: str
    candidate_hash: str
    status: str
    verified: bool
    tier_used: Optional[int]
    attempts: int
    cost_usd: float
    duration_ms: float
    created_at: str
    applied: bool = False
    applied_at: Optional[str] = None
    approved: bool = False
    approved_at: Optional[str] = None
    # Authorization provenance. These fields distinguish an interactive human
    # approval from an explicit automation flag; approved_at is kept for
    # backward compatibility with existing artifacts and consumers.
    approved_by: Optional[str] = None
    approval_source: Optional[str] = None
    approval_timestamp: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CandidateMetadata:
        return cls(
            run_id=data["run_id"],
            target_file=data["target_file"],
            test_command=data["test_command"],
            baseline_hash=data["baseline_hash"],
            candidate_hash=data["candidate_hash"],
            status=data["status"],
            verified=data["verified"],
            tier_used=data.get("tier_used"),
            attempts=data["attempts"],
            cost_usd=data["cost_usd"],
            duration_ms=data["duration_ms"],
            created_at=data["created_at"],
            applied=data.get("applied", False),
            applied_at=data.get("applied_at"),
            approved=data.get("approved", False),
            approved_at=data.get("approved_at"),
            approved_by=data.get("approved_by"),
            approval_source=data.get("approval_source"),
            approval_timestamp=data.get("approval_timestamp"),
        )


@dataclass
class ApplyResult:
    """Outcome of an authoritative patch application request."""
    success: bool
    run_id: str
    target_file: str
    message: str
    error_code: Optional[str] = None
    applied_at: Optional[str] = None
    mode_preserved: bool = False
    ownership_preserved: Optional[bool] = None


class CandidateArtifactStore:
    """Manages persistent, isolated per-run artifacts in `.argus/runs/<run_id>/`."""

    DEFAULT_DIR = ".argus"

    @classmethod
    def get_runs_dir(cls, workspace_root: Path) -> Path:
        return workspace_root / cls.DEFAULT_DIR / "runs"

    @classmethod
    def save_run_artifact(
        cls,
        run_id: str,
        target_file: str,
        test_command: str,
        baseline_hash: str,
        status: str,
        verified: bool,
        tier_used: Optional[int],
        attempts: int,
        cost_usd: float,
        duration_ms: float,
        patched_content: Optional[str],
        unified_diff: Optional[str],
        workspace_root: Optional[Path] = None,
        prompt_text: Optional[str] = None,
        raw_response: Optional[str] = None,
        parsed_patch: Optional[str] = None,
        context_window: Optional[Dict[str, Any]] = None,
    ) -> Path:
        """Persist candidate patch, forensic logs, and metadata into an isolated per-run folder."""
        root = (workspace_root or Path.cwd()).resolve()
        run_dir = cls.get_runs_dir(root) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        candidate_hash = ""
        if patched_content is not None:
            candidate_hash = hashlib.sha256(patched_content.encode("utf-8")).hexdigest()

        now_iso = datetime.now(timezone.utc).isoformat()
        metadata = CandidateMetadata(
            run_id=run_id,
            target_file=target_file,
            test_command=test_command,
            baseline_hash=baseline_hash,
            candidate_hash=candidate_hash,
            status=status,
            verified=verified,
            tier_used=tier_used,
            attempts=attempts,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            created_at=now_iso,
        )

        # 1. Write metadata.json
        meta_path = run_dir / "metadata.json"
        meta_path.write_text(json.dumps(metadata.to_dict(), indent=2), encoding="utf-8")

        # 2. Write candidate.patched (if present)
        if patched_content is not None:
            (run_dir / "candidate.patched").write_text(patched_content, encoding="utf-8")

        # 3. Write patch.diff (if present)
        if unified_diff:
            (run_dir / "patch.diff").write_text(unified_diff, encoding="utf-8")

        # 4. Write forensic artifacts (scrubbed to prevent credential retention on disk)
        if prompt_text:
            (run_dir / "prompt.txt").write_text(scrub_secrets(prompt_text), encoding="utf-8")
        if raw_response:
            (run_dir / "raw_response.txt").write_text(scrub_secrets(raw_response), encoding="utf-8")
        if parsed_patch and not unified_diff:
            (run_dir / "unapplied_patch.txt").write_text(scrub_secrets(parsed_patch), encoding="utf-8")
        if context_window:
            safe_context = scrub_object(context_window)
            (run_dir / "context_window.json").write_text(json.dumps(safe_context, indent=2), encoding="utf-8")

        # 5. Update pointer to most recent run
        last_run_file = root / cls.DEFAULT_DIR / "last_run_id"
        last_run_file.parent.mkdir(parents=True, exist_ok=True)
        last_run_file.write_text(run_id, encoding="utf-8")

        return run_dir

    @classmethod
    def resolve_run_dir(cls, run_id: str, workspace_root: Optional[Path] = None) -> Path:
        """Resolve run directory with support for exact name or unique prefix/suffix match."""
        root = (workspace_root or Path.cwd()).resolve()
        runs_dir = cls.get_runs_dir(root)
        direct = runs_dir / run_id
        if direct.is_dir():
            return direct
        if runs_dir.is_dir():
            matches = [
                d for d in runs_dir.iterdir()
                if d.is_dir() and (d.name.startswith(run_id) or d.name.endswith(run_id) or run_id in d.name)
            ]
            if len(matches) == 1:
                return matches[0]
            elif len(matches) > 1:
                raise CandidateNotFoundError(f"Ambiguous run identifier '{run_id}' ({len(matches)} matches)")
        raise CandidateNotFoundError(f"Run candidate '{run_id}' not found in {runs_dir}")

    @classmethod
    def load_candidate(
        cls,
        run_id: str,
        workspace_root: Optional[Path] = None,
    ) -> Tuple[CandidateMetadata, Optional[str]]:
        """Load metadata and candidate patched content for a specific run ID."""
        run_dir = cls.resolve_run_dir(run_id, workspace_root)

        meta_path = run_dir / "metadata.json"
        if not meta_path.is_file():
            raise CandidateNotFoundError(f"Run metadata not found at {meta_path}")

        try:
            meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
            metadata = CandidateMetadata.from_dict(meta_data)
        except Exception as ex:
            raise CandidateNotFoundError(f"Corrupted run metadata at {meta_path}: {ex}")

        patched_content: Optional[str] = None
        candidate_file = run_dir / "candidate.patched"
        if candidate_file.is_file():
            patched_content = candidate_file.read_text(encoding="utf-8")

        return metadata, patched_content

    @classmethod
    def get_last_run_id(cls, workspace_root: Optional[Path] = None) -> Optional[str]:
        """Read the last run ID recorded in the workspace."""
        root = (workspace_root or Path.cwd()).resolve()
        pointer = root / cls.DEFAULT_DIR / "last_run_id"
        if pointer.is_file():
            run_id = pointer.read_text(encoding="utf-8").strip()
            if run_id:
                return run_id
        return None

    @classmethod
    def list_candidates(cls, workspace_root: Optional[Path] = None) -> List[CandidateMetadata]:
        """List all candidate recovery runs found in the workspace."""
        root = (workspace_root or Path.cwd()).resolve()
        runs_dir = cls.get_runs_dir(root)
        if not runs_dir.is_dir():
            return []

        results: List[CandidateMetadata] = []
        for run_path in runs_dir.iterdir():
            if run_path.is_dir():
                meta_file = run_path / "metadata.json"
                if meta_file.is_file():
                    try:
                        data = json.loads(meta_file.read_text(encoding="utf-8"))
                        results.append(CandidateMetadata.from_dict(data))
                    except Exception:
                        pass
        # Sort by creation time descending
        results.sort(key=lambda m: m.created_at, reverse=True)
        return results

    @classmethod
    def get_candidate_diff(cls, run_id: str, workspace_root: Optional[Path] = None) -> str:
        """Read the stored unified diff for a candidate run."""
        try:
            run_dir = cls.resolve_run_dir(run_id, workspace_root)
            diff_file = run_dir / "patch.diff"
            if diff_file.is_file():
                return diff_file.read_text(encoding="utf-8")
        except CandidateNotFoundError:
            pass
        return ""

    @classmethod
    def set_approval(
        cls,
        run_id: str,
        approved: bool,
        workspace_root: Optional[Path] = None,
        approved_by: Optional[str] = None,
        approval_source: Optional[str] = None,
    ) -> CandidateMetadata:
        """Record approval or rejection with explicit authorization provenance.

        ``approved_by`` identifies the actor (for example ``interactive:cli``)
        and ``approval_source`` identifies the mechanism (for example
        ``cli-approve`` or ``automation:--yes-reviewed``). Automation sources
        must never be recorded as a human reviewer.
        """
        run_dir = cls.resolve_run_dir(run_id, workspace_root)
        meta_file = run_dir / "metadata.json"
        if not meta_file.is_file():
            raise CandidateNotFoundError(f"Run metadata not found for {run_id}")

        data = json.loads(meta_file.read_text(encoding="utf-8"))
        now_iso = datetime.now(timezone.utc).isoformat() if approved else None
        data["approved"] = approved
        data["approved_at"] = now_iso
        data["approved_by"] = approved_by if approved else None
        data["approval_source"] = approval_source if approved else None
        data["approval_timestamp"] = now_iso

        meta_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return CandidateMetadata.from_dict(data)


class AuthoritativePatchApplier:
    """Authoritatively applies verified candidate patches with atomic replace and TOCTOU defense."""

    @classmethod
    def apply(
        cls,
        run_id: str,
        workspace_root: Optional[str | Path] = None,
        expected_target: Optional[str | Path] = None,
        require_approval: bool = False,
    ) -> ApplyResult:
        """Apply a candidate patch to the target source file with strict verification.

        Safety Invariants Enforced:
        1. Run artifact must exist in `.argus/runs/<run_id>/`.
        2. Candidate must have status == 'SUCCESS' and verified == True.
        3. If require_approval is True, candidate must have approved == True.
        4. Target file must reside within workspace (no path traversal).
        5. Target file on disk must currently exist.
        6. Target file SHA-256 must match metadata baseline_hash (stale-patch defense).
        7. Candidate patched content must match metadata candidate_hash.
        8. If Python file, AST parsing must succeed.
        9. Temporary write is performed in the target directory (same filesystem).
        10. Target file hash is re-checked immediately before directory rename.
        11. Replacement is performed via atomic os.replace.
        12. Original file remains untouched if any step fails before os.replace.
        """
        root = Path(workspace_root or Path.cwd()).resolve()

        # Step 1: Load candidate artifact
        try:
            metadata, candidate_content = CandidateArtifactStore.load_candidate(run_id, workspace_root=root)
        except CandidateApplicationError as err:
            return ApplyResult(
                success=False,
                run_id=run_id,
                target_file=str(expected_target or "unknown"),
                error_code=err.error_code,
                message=err.message,
            )

        target_file_str = metadata.target_file

        # Step 2: Verification eligibility check
        if not metadata.verified or metadata.status != "SUCCESS":
            return ApplyResult(
                success=False,
                run_id=run_id,
                target_file=target_file_str,
                error_code="CANDIDATE_NOT_VERIFIED",
                message=f"Candidate from run {run_id} is not verified passing (status={metadata.status}, verified={metadata.verified}).",
            )

        # Step 2b: Human approval gate check (enforced for MCP and automated agents)
        if require_approval and not metadata.approved:
            return ApplyResult(
                success=False,
                run_id=run_id,
                target_file=target_file_str,
                error_code="CANDIDATE_NOT_APPROVED",
                message=(
                    f"Candidate '{run_id}' has not received human approval. "
                    f"Review the diff and run 'argus approve {run_id}' before applying."
                ),
            )

        if candidate_content is None:
            return ApplyResult(
                success=False,
                run_id=run_id,
                target_file=target_file_str,
                error_code="CANDIDATE_CONTENT_MISSING",
                message=f"No patched candidate content file found for run {run_id}.",
            )

        # Step 3: Workspace containment check (prevent path traversal).
        # Resolve the parent, not the final component: resolving the final
        # component follows symlinks, which would hide a symlink target from
        # the lstat() safety check below. A symlinked parent that escapes the
        # workspace is still rejected by the parent containment check.
        raw_target = root / target_file_str
        try:
            target_path = raw_target.parent.resolve() / raw_target.name
            target_path.relative_to(root)
        except (OSError, ValueError):
            return ApplyResult(
                success=False,
                run_id=run_id,
                target_file=target_file_str,
                error_code="PATH_TRAVERSAL_REJECTED",
                message=f"Target file {target_file_str} resolves outside workspace root {root}.",
            )

        # Step 4: Optional target assertion
        if expected_target:
            expected_resolved = (root / expected_target).resolve()
            if expected_resolved != target_path:
                return ApplyResult(
                    success=False,
                    run_id=run_id,
                    target_file=target_file_str,
                    error_code="TARGET_FILE_MISMATCH",
                    message=f"Target file mismatch: run targets '{target_file_str}', caller specified '{expected_target}'.",
                )

        # Step 5: Target file existence and type check
        if not target_path.is_file():
            return ApplyResult(
                success=False,
                run_id=run_id,
                target_file=target_file_str,
                error_code="TARGET_NOT_FOUND",
                message=f"Target file does not exist on disk: {target_path}",
            )

        # os.replace() replaces a symlink itself, not its destination. Refuse to
        # operate on symlinks or special files so application can never detach a
        # link or replace a device/FIFO. The original inode is also used to carry
        # permission and ownership metadata across the atomic replace.
        try:
            target_stat = target_path.lstat()
        except OSError as ex:
            return ApplyResult(
                success=False,
                run_id=run_id,
                target_file=target_file_str,
                error_code="READ_ERROR",
                message=f"Failed to inspect target file {target_path}: {ex}",
            )

        if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(target_stat.st_mode):
            return ApplyResult(
                success=False,
                run_id=run_id,
                target_file=target_file_str,
                error_code="UNSAFE_TARGET_TYPE",
                message=(
                    f"Refusing to apply to '{target_path.name}': target is a symlink "
                    "or special file. Only regular files are supported."
                ),
            )

        original_mode = stat.S_IMODE(target_stat.st_mode)

        # Step 6: Candidate content hash verification
        actual_candidate_hash = hashlib.sha256(candidate_content.encode("utf-8")).hexdigest()
        if actual_candidate_hash != metadata.candidate_hash:
            return ApplyResult(
                success=False,
                run_id=run_id,
                target_file=target_file_str,
                error_code="CONTENT_HASH_MISMATCH",
                message="Candidate content hash mismatch: candidate file on disk was modified or corrupted.",
            )

        # Step 7: Universal AST syntax check
        if target_path.suffix == ".py":
            ast_res = ASTChecker.validate_python_code(candidate_content, filename=str(target_path))
            if not ast_res.is_valid:
                return ApplyResult(
                    success=False,
                    run_id=run_id,
                    target_file=target_file_str,
                    error_code="SYNTAX_VALIDATION_FAILED",
                    message=f"Candidate code failed AST syntax check: {ast_res.formatted_error()}",
                )

        # Step 8: Pre-write baseline check
        try:
            target_bytes = target_path.read_bytes()
            current_target_hash = hashlib.sha256(target_bytes).hexdigest()
        except Exception as ex:
            return ApplyResult(
                success=False,
                run_id=run_id,
                target_file=target_file_str,
                error_code="READ_ERROR",
                message=f"Failed to read target file {target_path}: {ex}",
            )

        if current_target_hash != metadata.baseline_hash:
            return ApplyResult(
                success=False,
                run_id=run_id,
                target_file=target_file_str,
                error_code="STALE_TARGET_CONFLICT",
                message=(
                    f"Stale Target Conflict: Target file '{target_path.name}' was modified concurrently on disk. "
                    f"Baseline hash ({metadata.baseline_hash[:8]}...) != Current hash ({current_target_hash[:8]}...). "
                    "Application refused to prevent silent data loss."
                ),
            )

        # Step 9: Atomic replacement via temporary file in same directory
        temp_dir = target_path.parent
        temp_name = f".argus_tmp_{run_id}_{uuid.uuid4().hex[:8]}"
        temp_path = temp_dir / temp_name

        mode_preserved = False
        ownership_preserved: Optional[bool] = None

        try:
            with open(temp_path, "wb") as f:
                f.write(candidate_content.encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())

            # Step 9b: Preserve permission bits. A temporary file is created with
            # the process umask (typically 0600), so replacing without an explicit
            # chmod would silently downgrade executables and readable configs.
            try:
                os.chmod(temp_path, original_mode)
            except OSError as ex:
                return ApplyResult(
                    success=False,
                    run_id=run_id,
                    target_file=target_file_str,
                    error_code="PERMISSION_PRESERVATION_FAILED",
                    message=(
                        f"Could not preserve permission mode {oct(original_mode)} for "
                        f"'{target_path.name}': {ex}. Application aborted; original file preserved."
                    ),
                )

            if os.name == "posix":
                actual_mode = stat.S_IMODE(os.stat(temp_path).st_mode)
                if actual_mode != original_mode:
                    return ApplyResult(
                        success=False,
                        run_id=run_id,
                        target_file=target_file_str,
                        error_code="PERMISSION_PRESERVATION_FAILED",
                        message=(
                            f"Permission mode mismatch after chmod for '{target_path.name}': "
                            f"expected {oct(original_mode)}, got {oct(actual_mode)}. "
                            "Application aborted; original file preserved."
                        ),
                    )
            mode_preserved = True

            # Ownership preservation is best-effort: changing owner requires
            # privileges on POSIX and is not supported by Windows. Report the
            # limitation instead of silently claiming it was preserved. The mode
            # bits above remain mandatory.
            if os.name == "posix" and hasattr(os, "chown"):
                try:
                    os.chown(temp_path, target_stat.st_uid, target_stat.st_gid)
                    ownership_preserved = True
                except OSError:
                    ownership_preserved = False
            else:
                ownership_preserved = None

            # Step 10: Pre-replacement re-verification (minimize TOCTOU window)
            recheck_hash = hashlib.sha256(target_path.read_bytes()).hexdigest()
            if recheck_hash != metadata.baseline_hash:
                return ApplyResult(
                    success=False,
                    run_id=run_id,
                    target_file=target_file_str,
                    error_code="CONCURRENT_MODIFICATION_DETECTED",
                    message=(
                        f"Concurrent Modification Detected: Target file '{target_path.name}' was modified "
                        "while preparing patch application. Application aborted; original file preserved intact."
                    ),
                )

            # Step 11: Atomic replace (guaranteed atomic on same filesystem)
            os.replace(temp_path, target_path)

        except Exception as ex:
            return ApplyResult(
                success=False,
                run_id=run_id,
                target_file=target_file_str,
                error_code="APPLY_IO_ERROR",
                message=f"I/O error during file replacement: {ex}",
            )
        finally:
            # Guarantee temp file cleanup if replacement did not complete
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

        # Step 12: Record application in metadata
        applied_at = datetime.now(timezone.utc).isoformat()
        metadata.applied = True
        metadata.applied_at = applied_at
        try:
            meta_path = CandidateArtifactStore.get_runs_dir(root) / run_id / "metadata.json"
            meta_path.write_text(json.dumps(metadata.to_dict(), indent=2), encoding="utf-8")
        except Exception:
            pass  # Non-fatal if metadata update fails after successful code write

        ownership_note = ""
        if ownership_preserved is False:
            ownership_note = " Ownership was not preserved (insufficient privilege); mode bits were preserved."

        return ApplyResult(
            success=True,
            run_id=run_id,
            target_file=target_file_str,
            applied_at=applied_at,
            mode_preserved=mode_preserved,
            ownership_preserved=ownership_preserved,
            message=(
                f"Successfully applied verified candidate patch from run '{run_id}' "
                f"to '{target_path.name}' (mode {oct(original_mode)} preserved).{ownership_note}"
            ),
        )
