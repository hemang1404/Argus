"""Workspace Manager: runs tests in an isolated temporary copy of the project (no git pollution)."""

from __future__ import annotations

import difflib
import fnmatch
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set

from argus.patch.fuzzy_engine import PatchResult, ResilientPatchEngine
from argus.patch.parser import PatchBlock
from argus.patch.textio import decode_source, encode_source

# Names that are never part of a workspace snapshot, at any depth: VCS
# internals, Argus's own run directory, and bytecode caches.
ALWAYS_EXCLUDED_NAMES: Set[str] = {".git", ".argus", "__pycache__"}

# Heavy tool/venv caches skipped only when they sit at the project ROOT (a
# nested ``data/`` or ``build/`` directory is project content and is copied).
# Extend per run with ``WorkspaceManager(snapshot_exclude=...)``.
DEFAULT_SNAPSHOT_EXCLUDES: Set[str] = {
    ".venv",
    "venv",
    "node_modules",
    ".tox",
    ".nox",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".hg",
    ".svn",
}

# Secret-bearing files are never copied into the workspace snapshot by default
# (the tests run on a copy that may be left behind in a temp directory, and the
# model-visible tree should not carry credentials). Matched on the file name at
# any depth. ``.env.example``-style templates are not secrets and are kept. Set
# ``snapshot_include_secrets=True`` (config) when the tests genuinely need them.
SECRET_FILE_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    ".netrc",
    "_netrc",
    ".pypirc",
    ".npmrc",
    ".git-credentials",
    "secrets.json",
    "credentials.json",
)
SECRET_FILE_ALLOWED_SUFFIXES = (".example", ".sample", ".template", ".dist")


def is_secret_file_name(name: str) -> bool:
    """Whether ``name`` looks like a credential file that snapshots leave out."""
    lowered = name.lower()
    if lowered.endswith(SECRET_FILE_ALLOWED_SUFFIXES):
        return False
    return any(fnmatch.fnmatchcase(lowered, pat) for pat in SECRET_FILE_PATTERNS)


# Upper bound on the copied project size, so a mis-rooted run (for example a
# home directory) fails fast instead of copying everything. None disables it.
DEFAULT_SNAPSHOT_MAX_BYTES: int = 2 * 1024**3


def _handle_remove_readonly(func, path, exc_info):
    """Handle read-only files on Windows during directory removal."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def _ps_create_time(pid: int) -> Optional[float]:
    """Process start time (Unix epoch seconds) via ``ps -o lstart=``; used where /proc does not exist (macOS, BSD).

    ``lstart`` has one-second resolution and is printed in the local time zone, which is
    consistent with the 2 s tolerance in :func:`is_process_alive`. Returns None when ``ps``
    is missing, the process is gone, or the output cannot be parsed.
    """
    try:
        env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
        res = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5, env=env)
        text = " ".join(res.stdout.split())
        if res.returncode != 0 or not text:
            return None
        return time.mktime(time.strptime(text, "%a %b %d %H:%M:%S %Y"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def get_process_create_time(pid: int) -> Optional[float]:
    """Get process creation timestamp (Unix epoch seconds) if accessible."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return None
            try:
                ct, et, kt, ut = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
                if kernel32.GetProcessTimes(handle, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)):
                    ft = (ct.dwHighDateTime << 32) + ct.dwLowDateTime
                    # Convert Windows FILETIME (100-ns intervals since 1601) to Unix epoch seconds
                    return (ft - 116444736000000000) / 10000000.0
                return None
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    else:
        try:
            stat_file = Path(f"/proc/{pid}/stat")
            if stat_file.exists():
                content = stat_file.read_text()
                # Field 2 (comm) can contain spaces and parentheses; locate closing parenthesis
                rest = content[content.rfind(")") + 2:].split()
                # Field 22 (starttime in clock ticks since system boot) is index 19
                return float(rest[19])
        except Exception:
            pass
        try:
            proc_stat = Path(f"/proc/{pid}")
            if proc_stat.exists():
                return proc_stat.stat().st_ctime
        except Exception:
            pass
        return _ps_create_time(pid)


def is_process_alive(pid: int, expected_create_time: Optional[float] = None) -> bool:
    """Check if process with given PID is currently alive on the host.

    If expected_create_time is provided, also validates that the live process has
    a matching creation timestamp, preventing false positives from PID reuse.
    """
    if pid <= 0:
        return False
    alive = False
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                STILL_ACTIVE = 259
                exit_code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    alive = (exit_code.value == STILL_ACTIVE)
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError as err:
            import errno
            if err.errno == errno.EPERM:
                alive = True  # Process exists but owned by different user; fail safe assume alive
            else:
                return False

    if not alive:
        return False

    # Process identity verification against PID reuse
    if expected_create_time is not None:
        actual_create_time = get_process_create_time(pid)
        if actual_create_time is not None:
            # Allow up to 2.0s clock drift or filesystem resolution difference
            if abs(actual_create_time - expected_create_time) > 2.0:
                # PID has been recycled by the OS for a different process!
                return False

    return True


def cleanup_stale_temp_workspaces(max_age_hours: float = 1.0) -> int:
    """Prune dangling argus_ws_* ephemeral directories left by aborted/crashed runs.

    Guarantees session safety:
      - Never removes an active workspace whose owning process is alive and identity matches.
      - Never removes recent workspaces if ownership metadata is unobservable or temporarily locked.
      - Uses atomic rename to prevent races between concurrent garbage collectors.
    """
    cleaned = 0
    now = time.time()
    temp_dir = Path(tempfile.gettempdir())
    for ws_dir in temp_dir.glob("argus_ws_*"):
        try:
            # Check for active session metadata
            session_file = ws_dir / ".argus_session.json"
            if session_file.exists():
                try:
                    data = json.loads(session_file.read_text(encoding="utf-8"))
                    owner_pid = data.get("pid")
                    expected_ctime = data.get("process_create_time")
                    session_created_at = data.get("created_at", 0.0)

                    # 1. If owning process is demonstrably alive and identity matches: preserve!
                    if owner_pid and is_process_alive(int(owner_pid), expected_create_time=expected_ctime):
                        continue

                    # 2. If owning process is temporarily unobservable or session is fresh (< max_age_hours):
                    # Fail-safe: do not delete recent workspaces if liveness cannot be disproven
                    if (now - session_created_at) < (max_age_hours * 3600):
                        continue
                except Exception:
                    # Incomplete or temporarily locked session metadata: fail safe and preserve
                    continue

            mtime = ws_dir.stat().st_mtime
            if (now - mtime) > (max_age_hours * 3600):
                # Atomic claim via rename to prevent concurrent GC collisions
                claim_target = ws_dir.with_name(f"{ws_dir.name}_deleting_{uuid.uuid4().hex[:6]}")
                try:
                    ws_dir.rename(claim_target)
                except (OSError, PermissionError):
                    # Directory is actively open or claimed by another process
                    continue
                shutil.rmtree(claim_target, onerror=_handle_remove_readonly)
                cleaned += 1
        except Exception:
            pass
    return cleaned


@dataclass
class WorkspaceSnapshot:
    """State metadata for an active isolated workspace."""
    workspace_path: Path
    repo_root: Path
    relative_target_file: Path
    original_content: str


class TargetNotFoundError(FileNotFoundError):
    """The configured target file does not exist."""


class SnapshotTooLargeError(ValueError):
    """The project snapshot would exceed the configured size limit."""


class TargetOutsideWorkspaceError(ValueError):
    """The target file lies outside the workspace root; Argus will not re-root the snapshot."""


def _is_excluded(rel_parts: Sequence[str], root_level_excludes: Set[str], protect_secrets: bool = True) -> bool:
    """Whether a snapshot-relative path is left out of the workspace copy."""
    if not rel_parts:
        return False
    if any(part in ALWAYS_EXCLUDED_NAMES for part in rel_parts):
        return True
    if rel_parts[0] in root_level_excludes:
        return True
    if protect_secrets and any(is_secret_file_name(part) for part in rel_parts):
        return True
    return rel_parts[-1].endswith((".pyc", ".pyo"))


def _walk_snapshot_files(root: Path, base: Path, excludes: Set[str], protect_secrets: bool = True) -> List[Path]:
    """All files and symlinks below ``root`` (relative to ``base``), honouring excludes."""
    found: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        rel_dir = here.relative_to(base)
        keep: List[str] = []
        for d in dirnames:
            rel = rel_dir / d
            if _is_excluded(rel.parts, excludes, protect_secrets):
                continue
            if (here / d).is_symlink():
                found.append(rel)  # linked directory: copied as a link, never followed
            else:
                keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            rel = rel_dir / f
            if not _is_excluded(rel.parts, excludes, protect_secrets):
                found.append(rel)
    return found


def _git_snapshot_files(
    root: Path, excludes: Set[str], timeout: int, protect_secrets: bool = True
) -> Optional[List[Path]]:
    """Tracked plus untracked-but-not-ignored files per git, or None if unavailable."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=str(root),
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    files: List[Path] = []
    seen: Set[Path] = set()
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        rel = Path(os.fsdecode(raw))
        if rel in seen or _is_excluded(rel.parts, excludes, protect_secrets):
            continue
        seen.add(rel)
        src = root / rel
        if src.is_symlink() or src.is_file():
            files.append(rel)
        elif src.is_dir():
            # submodule / nested repository: take its working files directly
            files.extend(p for p in _walk_snapshot_files(src, root, excludes, protect_secrets) if p not in seen)
        # tracked-but-deleted paths simply do not exist and are skipped
    return files


def _copy_snapshot_entry(root: Path, dest_root: Path, rel: Path) -> None:
    src = root / rel
    dst = dest_root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        link = os.readlink(src)
        resolved = (src.parent / link).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            return  # never materialise or expose something outside the workspace root
        try:
            os.symlink(link, dst, target_is_directory=src.is_dir())
        except (OSError, NotImplementedError):
            if resolved.is_file():
                shutil.copy2(resolved, dst)
        return
    shutil.copy2(src, dst)


class WorkspaceManager:
    """Manages an ephemeral directory copy for non-destructive test execution.

    The snapshot is a copy of the project: every file git tracks, plus
    untracked files that are not git-ignored (so fixtures such as ``data/``,
    ``tests/data/`` or ``*.zip`` are present). Outside a git repository, or with
    ``snapshot_respect_gitignore=False``, everything is copied. Always left out:
    ``.git``, ``.argus`` (the run directory), ``__pycache__``/``*.pyc`` and the
    root-level heavy caches in ``DEFAULT_SNAPSHOT_EXCLUDES`` (configurable via
    ``snapshot_exclude``). Files are copied byte-for-byte; the target is read and
    written with its own encoding, BOM and line endings.
    """

    def __init__(
        self,
        repo_root: Optional[Path] = None,
        target_file: Optional[Path | str] = None,
        test_file: Optional[Path | str] = None,
        use_worktree: bool = False,
        snapshot_exclude: Optional[Iterable[str]] = None,
        snapshot_respect_gitignore: bool = True,
        snapshot_max_bytes: Optional[int] = DEFAULT_SNAPSHOT_MAX_BYTES,
        snapshot_include_secrets: bool = False,
    ) -> None:
        if use_worktree:
            raise ValueError(
                "Git worktree isolation was removed: Argus recovery runs in a temporary "
                "directory copy of the project. Do not pass use_worktree."
            )
        self.snapshot_excludes: Set[str] = set(DEFAULT_SNAPSHOT_EXCLUDES) | set(snapshot_exclude or ())
        self.snapshot_respect_gitignore = snapshot_respect_gitignore
        self.snapshot_max_bytes = snapshot_max_bytes
        self.snapshot_protect_secrets = not snapshot_include_secrets
        self.git_timeout: int = 30
        if repo_root:
            self.repo_root = Path(repo_root).resolve()
            if target_file:
                tf_candidate = Path(target_file)
                tf = (
                    tf_candidate.resolve()
                    if tf_candidate.is_absolute()
                    else (self.repo_root / tf_candidate).resolve()
                )
                try:
                    tf.relative_to(self.repo_root)
                except ValueError:
                    raise TargetOutsideWorkspaceError(
                        f"Target file {tf} is outside the workspace root {self.repo_root}. "
                        "Argus will not snapshot a different directory; run it from the "
                        "project that contains the target."
                    ) from None
        elif target_file and test_file:
            tf = Path(target_file).resolve()
            tt = Path(test_file).resolve()
            try:
                if tf.drive == tt.drive:
                    common = Path(os.path.commonpath([str(tf), str(tt)]))
                    self.repo_root = common
                else:
                    self.repo_root = tf.parent
            except Exception:
                self.repo_root = tf.parent
        elif target_file:
            tf = Path(target_file).resolve()
            cwd = Path.cwd().resolve()
            try:
                tf.relative_to(cwd)
                self.repo_root = cwd
            except ValueError:
                self.repo_root = tf.parent
        else:
            self.repo_root = Path.cwd().resolve()

        self.target_file_rel = Path(target_file) if target_file else None
        if self.target_file_rel and self.target_file_rel.is_absolute():
            try:
                self.target_file_rel = self.target_file_rel.resolve().relative_to(self.repo_root)
            except ValueError:
                self.target_file_rel = Path(self.target_file_rel.name)

        self.test_file_rel = Path(test_file) if test_file else None
        if self.test_file_rel and self.test_file_rel.is_absolute():
            try:
                self.test_file_rel = self.test_file_rel.resolve().relative_to(self.repo_root)
            except ValueError:
                self.test_file_rel = Path(self.test_file_rel.name)

        self.temp_dir: Optional[Path] = None
        self._original_content: Optional[str] = None
        self._encoding: str = "utf-8"
        self._bom: bytes = b""
        self.session_id: str = uuid.uuid4().hex[:12]

    @property
    def workspace_root(self) -> Path:
        """Return the active isolated workspace path (or fallback to repo_root)."""
        return self.temp_dir if self.temp_dir is not None else self.repo_root

    def __enter__(self) -> WorkspaceManager:
        self.setup()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.cleanup()

    def _snapshot_file_list(self) -> List[Path]:
        if self.snapshot_respect_gitignore:
            listed = _git_snapshot_files(
                self.repo_root, self.snapshot_excludes, self.git_timeout, self.snapshot_protect_secrets
            )
            if listed is not None:
                return listed
        return _walk_snapshot_files(
            self.repo_root, self.repo_root, self.snapshot_excludes, self.snapshot_protect_secrets
        )

    def setup(self) -> Path:
        """Create the ephemeral workspace copy and record the target's baseline."""
        # Auto-prune any orphaned temporary workspaces from crashed/aborted previous sessions
        cleanup_stale_temp_workspaces()

        self.temp_dir = Path(tempfile.mkdtemp(prefix="argus_ws_"))
        try:
            files = self._snapshot_file_list()
            if self.snapshot_max_bytes is not None:
                total = 0
                for rel in files:
                    try:
                        total += (self.repo_root / rel).lstat().st_size
                    except OSError:
                        continue
                    if total > self.snapshot_max_bytes:
                        raise SnapshotTooLargeError(
                            f"Project snapshot of {self.repo_root} exceeds {self.snapshot_max_bytes} bytes. "
                            "Point Argus at the project directory (not a parent such as your home "
                            "directory), add heavy directories to snapshot_exclude, or raise "
                            "snapshot_max_bytes."
                        )
            for rel in files:
                _copy_snapshot_entry(self.repo_root, self.temp_dir, rel)

            # The target (and test file) are always present, even if git-ignored.
            for rel in (self.target_file_rel, self.test_file_rel):
                if rel is None:
                    continue
                dest = self.temp_dir / rel
                if not dest.exists():
                    orig = self.repo_root / rel
                    if not orig.is_file():
                        if rel is self.target_file_rel:
                            raise TargetNotFoundError(f"Target file not found: {orig}")
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(orig, dest)

            try:
                session_meta = {
                    "session_id": self.session_id,
                    "pid": os.getpid(),
                    "process_create_time": get_process_create_time(os.getpid()),
                    "created_at": time.time(),
                    "repo_root": str(self.repo_root),
                }
                (self.temp_dir / ".argus_session.json").write_text(json.dumps(session_meta), encoding="utf-8")
            except OSError:
                pass

            if self.target_file_rel:
                decoded = decode_source(
                    (self.temp_dir / self.target_file_rel).read_bytes(), name=str(self.target_file_rel)
                )
                self._original_content = decoded.text
                self._encoding = decoded.encoding
                self._bom = decoded.bom
        except BaseException:
            self.cleanup()
            raise

        return self.temp_dir

    def _target_path(self) -> Path:
        if not self.temp_dir or not self.target_file_rel:
            raise RuntimeError("Workspace not initialized or no target file configured.")
        return self.temp_dir / self.target_file_rel

    def _write_text(self, content: str) -> None:
        data = encode_source(content, self._encoding, self._bom, name=str(self.target_file_rel))
        self._target_path().write_bytes(data)

    def read_target_file(self) -> str:
        """Current text of the target in the workspace (BOM stripped, line endings as on disk)."""
        decoded = decode_source(self._target_path().read_bytes(), name=str(self.target_file_rel))
        return decoded.text

    def write_target_file(self, content: str) -> None:
        """Write ``content`` to the workspace target using the file's own encoding/BOM, no newline translation."""
        self._write_text(content)

    def apply_patch(self, patch: PatchBlock, engine: ResilientPatchEngine) -> PatchResult:
        """Apply a patch block to the target file inside the isolated workspace."""
        current_content = self.read_target_file()
        res = engine.apply_patch(current_content, patch, filename=str(self.target_file_rel))
        if res.success and res.patched_code is not None:
            self._write_text(res.patched_code)
        return res

    def reset_target_file(self) -> None:
        """Reset the target file in the isolated workspace back to original baseline content."""
        if self._original_content is not None and self.temp_dir and self.target_file_rel:
            self._write_text(self._original_content)

    def generate_unified_diff(self) -> str:
        """Generate a unified diff between the original file and current isolated state."""
        if not self.temp_dir or not self.target_file_rel or self._original_content is None:
            return ""

        current_content = self.read_target_file()
        orig_lines = self._original_content.splitlines(keepends=True)
        curr_lines = current_content.splitlines(keepends=True)

        diff = difflib.unified_diff(
            orig_lines,
            curr_lines,
            fromfile=f"a/{self.target_file_rel.as_posix()}",
            tofile=f"b/{self.target_file_rel.as_posix()}",
        )
        return "".join(diff)

    def cleanup(self) -> None:
        """Remove the ephemeral workspace directory."""
        if self.temp_dir and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, onerror=_handle_remove_readonly)
        self.temp_dir = None
