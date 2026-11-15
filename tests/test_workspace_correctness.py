"""Tests for WorkspaceManager: Full project snapshots and sibling imports."""

import tempfile
from pathlib import Path


from argus.runtime.workspace import WorkspaceManager


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
