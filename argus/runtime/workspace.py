"""Workspace Manager: Virtualizes execution into an isolated temporary copy ensuring 0 git pollution."""

from __future__ import annotations

import difflib
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
from typing import List, Optional, Set

from argus.patch.fuzzy_engine import PatchResult, ResilientPatchEngine
from argus.patch.parser import PatchBlock


# Directory names to exclude during full project snapshots
IGNORED_SNAPSHOT_DIRS: Set[str] = {
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".argus",
    "dist",
    "build",
    "logs",
    "scratch",
    "data",
    "packages",
    "experiments_archive",
    "results",
    "figures",
}


def _handle_remove_readonly(func, path, exc_info):
    """Handle read-only files on Windows during directory removal."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


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
        return None


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


def cleanup_stale_worktrees(repo_root: Path) -> int:
    """Prune dangling Git worktrees left by crashed, interrupted, or aborted processes."""
    repo_root = Path(repo_root).resolve()
    git_dir = repo_root / ".git"
    if not git_dir.exists():
        return 0

    cleaned_count = 0
    try:
        # First let git prune any worktrees whose target directory was deleted
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(repo_root),
            capture_output=True,
            timeout=10,
        )

        # Inspect any remaining worktrees
        res = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                if line.startswith("worktree "):
                    wt_path = line.split(" ", 1)[1].strip()
                    if "argus_wt_" in wt_path:
                        try:
                            subprocess.run(
                                ["git", "worktree", "remove", "--force", wt_path],
                                cwd=str(repo_root),
                                capture_output=True,
                                timeout=10,
                            )
                            cleaned_count += 1
                        except Exception:
                            pass

        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(repo_root),
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass

    return cleaned_count


@dataclass
class WorkspaceSnapshot:
    """State metadata for an active isolated workspace."""
    workspace_path: Path
    repo_root: Path
    relative_target_file: Path
    original_content: str


class WorkspaceManager:
    """Manages ephemeral directory copies or Git worktrees for non-destructive test execution.

    Preserves full project snapshots, sibling module imports, uncommitted changes,
    and provides robust worktree cleanup.
    """

    def __init__(
        self,
        repo_root: Optional[Path] = None,
        target_file: Optional[Path | str] = None,
        test_file: Optional[Path | str] = None,
        use_worktree: bool = False,
    ) -> None:
        self.use_worktree = use_worktree
        self._is_git_worktree = False
        if repo_root:
            self.repo_root = Path(repo_root).resolve()
            if target_file:
                tf_candidate = Path(target_file)
                if not tf_candidate.is_absolute():
                    tf = (self.repo_root / tf_candidate).resolve()
                else:
                    tf = tf_candidate.resolve()
                try:
                    tf.relative_to(self.repo_root)
                except ValueError:
                    if test_file:
                        tt_cand = Path(test_file)
                        tt = (self.repo_root / tt_cand).resolve() if not tt_cand.is_absolute() else tt_cand.resolve()
                        try:
                            if tf.drive == tt.drive:
                                self.repo_root = Path(os.path.commonpath([str(tf), str(tt)]))
                            else:
                                self.repo_root = tf.parent
                        except Exception:
                            self.repo_root = tf.parent
                    else:
                        self.repo_root = tf.parent
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
                self.target_file_rel = self.target_file_rel.relative_to(self.repo_root)
            except ValueError:
                self.target_file_rel = Path(self.target_file_rel.name)

        self.test_file_rel = Path(test_file) if test_file else None
        if self.test_file_rel and self.test_file_rel.is_absolute():
            try:
                self.test_file_rel = self.test_file_rel.relative_to(self.repo_root)
            except ValueError:
                self.test_file_rel = Path(self.test_file_rel.name)

        self.temp_dir: Optional[Path] = None
        self._original_content: Optional[str] = None
        self.session_id: str = uuid.uuid4().hex[:12]
        self.worktree_timeout: int = 30

    @property
    def workspace_root(self) -> Path:
        """Return the active isolated workspace path (or fallback to repo_root)."""
        return self.temp_dir if self.temp_dir is not None else self.repo_root

    def __enter__(self) -> WorkspaceManager:
        self.setup()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.cleanup()

    def setup(self) -> Path:
        """Create an ephemeral workspace or Git worktree with complete project snapshot."""
        # Auto-prune any orphaned temporary workspaces from crashed/aborted previous sessions
        cleanup_stale_temp_workspaces()

        if self.use_worktree:
            git_dir = self.repo_root / ".git"
            if git_dir.exists():
                cleanup_stale_worktrees(self.repo_root)
                self.temp_dir = Path(tempfile.mkdtemp(prefix="argus_wt_"))
                try:
                    self.temp_dir.rmdir()
                except Exception:
                    pass

                proc = subprocess.run(
                    ["git", "worktree", "add", "--detach", str(self.temp_dir), "HEAD"],
                    cwd=str(self.repo_root),
                    capture_output=True,
                    text=True,
                    timeout=self.worktree_timeout,
                )
                if proc.returncode == 0:
                    self._is_git_worktree = True
                    # Record session ownership metadata
                    try:
                        session_meta = {
                            "session_id": self.session_id,
                            "pid": os.getpid(),
                            "process_create_time": get_process_create_time(os.getpid()),
                            "created_at": time.time(),
                            "repo_root": str(self.repo_root),
                        }
                        (self.temp_dir / ".argus_session.json").write_text(json.dumps(session_meta), encoding="utf-8")
                    except Exception:
                        pass
                    # Overlay uncommitted working tree modifications and untracked files
                    self._overlay_uncommitted_changes()
                    if self.target_file_rel:
                        src_path = self.temp_dir / self.target_file_rel
                        if src_path.exists():
                            self._original_content = src_path.read_text(encoding="utf-8", errors="replace")
                    return self.temp_dir

        # Fallback / Default: Full isolated snapshot directory copy
        self.temp_dir = Path(tempfile.mkdtemp(prefix="argus_ws_"))

        def _ignore_heavy(directory: str, contents: list[str]) -> list[str]:
            ignored = []
            for item in contents:
                if item in IGNORED_SNAPSHOT_DIRS:
                    ignored.append(item)
                elif item.endswith((".pyc", ".pyo", ".pyd", ".docx", ".pdf", ".zip", ".tar", ".gz")):
                    ignored.append(item)
            return ignored

        # Perform deep copy of project root to preserve sibling modules, imports, configs
        shutil.copytree(
            self.repo_root,
            self.temp_dir,
            ignore=_ignore_heavy,
            dirs_exist_ok=True,
        )

        # Record session ownership metadata
        try:
            session_meta = {
                "session_id": self.session_id,
                "pid": os.getpid(),
                "process_create_time": get_process_create_time(os.getpid()),
                "created_at": time.time(),
                "repo_root": str(self.repo_root),
            }
            (self.temp_dir / ".argus_session.json").write_text(json.dumps(session_meta), encoding="utf-8")
        except Exception:
            pass

        # If a target file is specified, record its baseline content
        if self.target_file_rel:
            dest_target = self.temp_dir / self.target_file_rel
            if not dest_target.exists():
                # Check original repo root
                orig_target = self.repo_root / self.target_file_rel
                if not orig_target.exists():
                    raise FileNotFoundError(f"Target file not found: {orig_target}")
                dest_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(orig_target, dest_target)
            self._original_content = dest_target.read_text(encoding="utf-8", errors="replace")

        return self.temp_dir

    def _overlay_uncommitted_changes(self) -> None:
        """Sync uncommitted changes (dirty files and untracked files) into the worktree."""
        if not self.temp_dir or not self.temp_dir.exists():
            return

        try:
            res = subprocess.run(
                ["git", "status", "--porcelain", "-uall"],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if res.returncode != 0:
                return

            for line in res.stdout.splitlines():
                if len(line) < 4:
                    continue
                status_code = line[:2]
                rel_path_str = line[3:].strip().strip('"')
                # Handle renames e.g. "R  old -> new"
                if " -> " in rel_path_str:
                    rel_path_str = rel_path_str.split(" -> ")[-1].strip().strip('"')

                rel_path = Path(rel_path_str)
                # Skip heavy/ignored directories
                parts = rel_path.parts
                if any(part in IGNORED_SNAPSHOT_DIRS for part in parts):
                    continue

                src_file = self.repo_root / rel_path
                dest_file = self.temp_dir / rel_path

                if "D" in status_code:
                    # File was deleted in working tree
                    if dest_file.exists():
                        dest_file.unlink()
                elif src_file.is_file():
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dest_file)
        except Exception:
            pass

    def read_target_file(self) -> str:
        """Read the current contents of the target file in the isolated workspace."""
        if not self.temp_dir or not self.target_file_rel:
            raise RuntimeError("Workspace not initialized or no target file configured.")
        target_path = self.temp_dir / self.target_file_rel
        return target_path.read_text(encoding="utf-8", errors="replace")

    def write_target_file(self, content: str) -> None:
        """Write updated code content to the target file in the isolated workspace."""
        if not self.temp_dir or not self.target_file_rel:
            raise RuntimeError("Workspace not initialized or no target file configured.")
        target_path = self.temp_dir / self.target_file_rel
        target_path.write_text(content, encoding="utf-8")

    def apply_patch(self, patch: PatchBlock, engine: ResilientPatchEngine) -> PatchResult:
        """Apply a patch block to the target file inside the isolated workspace."""
        if not self.temp_dir or not self.target_file_rel:
            raise RuntimeError("Workspace not initialized or no target file configured.")

        target_path = self.temp_dir / self.target_file_rel
        current_content = target_path.read_text(encoding="utf-8", errors="replace")

        res = engine.apply_patch(current_content, patch, filename=str(self.target_file_rel))
        if res.success and res.patched_code is not None:
            target_path.write_text(res.patched_code, encoding="utf-8")

        return res

    def reset_target_file(self) -> None:
        """Reset the target file in the isolated workspace back to original baseline content."""
        if self._original_content is not None and self.temp_dir and self.target_file_rel:
            target_path = self.temp_dir / self.target_file_rel
            target_path.write_text(self._original_content, encoding="utf-8")

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
        """Remove the ephemeral workspace directory or Git worktree."""
        if getattr(self, "_is_git_worktree", False) and self.temp_dir:
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(self.temp_dir)],
                    cwd=str(self.repo_root),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=str(self.repo_root),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except Exception:
                pass
            self._is_git_worktree = False
        if self.temp_dir and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, onerror=_handle_remove_readonly)
            self.temp_dir = None
