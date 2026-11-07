"""Tests for WorkspaceManager: Full project snapshots, sibling imports, uncommitted changes, and worktree GC."""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from argus.patch.fuzzy_engine import ResilientPatchEngine
from argus.patch.parser import PatchBlock
from argus.runtime.workspace import WorkspaceManager, cleanup_stale_worktrees


def test_workspace_snapshot_preserves_sibling_module_imports():
    """Verify that ephemeral workspace snapshot copies the entire project, allowing sibling imports."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        repo_root = Path(tmp_dir).resolve()
        pkg_dir = repo_root / "mypkg"
        pkg_dir.mkdir()

        (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
        (pkg_dir / "sibling.py").write_text("VALUE = 42\n", encoding="utf-8")
        target_file = pkg_dir / "target.py"
        target_file.write_text("from mypkg.sibling import VALUE\n\ndef get_val():\n    return VALUE\n", encoding="utf-8")

        test_file = repo_root / "test_target.py"
        test_file.write_text("from mypkg.target import get_val\n\ndef test_val():\n    assert get_val() == 42\n", encoding="utf-8")

        with WorkspaceManager(repo_root=repo_root, target_file=target_file, test_file=test_file) as ws:
            isolated_target = ws.temp_dir / "mypkg" / "target.py"
            isolated_sibling = ws.temp_dir / "mypkg" / "sibling.py"
            isolated_test = ws.temp_dir / "test_target.py"

            assert isolated_target.exists(), "Target file must exist in isolated workspace"
            assert isolated_sibling.exists(), "Sibling module must exist in isolated workspace"
            assert isolated_test.exists(), "Test file must exist in isolated workspace"
            assert "VALUE = 42" in isolated_sibling.read_text(encoding="utf-8")


def test_git_worktree_overlays_uncommitted_changes_and_untracked_files():
    """Verify that git worktree mode overlays uncommitted edits and untracked files without altering repo."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        repo_root = Path(tmp_dir).resolve()

        # Initialize git repo
        subprocess.run(["git", "init"], cwd=str(repo_root), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(repo_root), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_root), check=True)

        # Baseline committed file
        tracked_file = repo_root / "module.py"
        tracked_file.write_text("def run():\n    return 'committed'\n", encoding="utf-8")
        subprocess.run(["git", "add", "module.py"], cwd=str(repo_root), check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo_root), check=True)

        # 1. Dirty uncommitted modification
        tracked_file.write_text("def run():\n    return 'dirty_edit'\n", encoding="utf-8")

        # 2. Untracked new helper file
        untracked_helper = repo_root / "helper.py"
        untracked_helper.write_text("SECRET_VAL = 999\n", encoding="utf-8")

        # Create worktree workspace
        with WorkspaceManager(repo_root=repo_root, target_file=tracked_file, use_worktree=True) as ws:
            assert ws._is_git_worktree is True
            wt_module = ws.temp_dir / "module.py"
            wt_helper = ws.temp_dir / "helper.py"

            # Both uncommitted changes and untracked files must be present in the worktree
            assert wt_module.exists()
            assert "dirty_edit" in wt_module.read_text(encoding="utf-8")
            assert wt_helper.exists()
            assert "SECRET_VAL = 999" in wt_helper.read_text(encoding="utf-8")

            # Apply a patch inside worktree
            engine = ResilientPatchEngine()
            patch = PatchBlock(file_path="module.py", search_text="return 'dirty_edit'", replace_text="return 'patched_clean'")
            res = ws.apply_patch(patch, engine)
            assert res.success is True
            assert "patched_clean" in wt_module.read_text(encoding="utf-8")

            # Verify original working tree is NOT polluted
            assert "dirty_edit" in tracked_file.read_text(encoding="utf-8")

        # After cleanup, worktree is removed
        assert not ws.temp_dir or not ws.temp_dir.exists()


def test_cleanup_stale_worktrees_prunes_abandoned_worktrees():
    """Verify that cleanup_stale_worktrees prunes orphaned worktree registrations."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        repo_root = Path(tmp_dir).resolve()
        subprocess.run(["git", "init"], cwd=str(repo_root), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(repo_root), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_root), check=True)

        f = repo_root / "main.py"
        f.write_text("print(1)\n", encoding="utf-8")
        subprocess.run(["git", "add", "main.py"], cwd=str(repo_root), check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_root), check=True)

        # Create an abandoned worktree manually
        abandoned_wt = repo_root / "argus_wt_abandoned"
        subprocess.run(["git", "worktree", "add", "--detach", str(abandoned_wt), "HEAD"], cwd=str(repo_root), check=True)

        # Delete the folder to simulate a crash/reboot
        import shutil
        shutil.rmtree(abandoned_wt, ignore_errors=True)

        # Now run cleanup_stale_worktrees
        cleaned = cleanup_stale_worktrees(repo_root)
        assert cleaned >= 0

        # Verify git worktree list has no missing worktrees
        res = subprocess.run(["git", "worktree", "list"], cwd=str(repo_root), capture_output=True, text=True)
        assert "argus_wt_abandoned" not in res.stdout

