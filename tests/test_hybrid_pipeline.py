"""Unit tests for Argus Deterministic Hybrid Pipeline (Localizer)."""

from pathlib import Path
import pytest

from argus.localization.localizer import DeterministicLocalizer


@pytest.fixture
def sample_repo(tmp_path: Path):
    """Create a mock repository structure for testing localization."""
    pkg_dir = tmp_path / "mypackage"
    pkg_dir.mkdir(parents=True)
    
    code = """# Module header
import os

class TableHandler:
    def __init__(self, name):
        self.name = name

    def write_table(self, data, format="ascii"):
        \"\"\"Write table data to format.\"\"\"
        if format == "unsupported":
            raise ValueError("Unsupported format")
        return f"Wrote {len(data)} items in {format}"

def standalone_helper(x):
    return x * 2
"""
    mod_file = pkg_dir / "handler.py"
    mod_file.write_text(code, encoding="utf-8")

    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    (test_dir / "test_handler.py").write_text("def test_dummy(): pass", encoding="utf-8")

    return tmp_path, mod_file


def test_symbol_extraction():
    localizer = DeterministicLocalizer(Path("."))
    issue_text = "Error calling `TableHandler.write_table` with `ValueError`: unsupported format in mypackage/handler.py"
    
    symbols = localizer.extract_symbols(issue_text)
    assert any("write_table" in s for s in symbols)
    assert any("ValueError" in s for s in symbols)
    assert any("TableHandler" in s for s in symbols)


def test_explicit_file_path_extraction(sample_repo):
    repo_dir, mod_file = sample_repo
    localizer = DeterministicLocalizer(repo_dir)
    
    text = "There is a bug in mypackage/handler.py when running write_table."
    found = localizer.extract_explicit_file_paths(text)
    assert len(found) == 1
    assert found[0].name == "handler.py"


def test_traceback_frame_localization(sample_repo):
    repo_dir, mod_file = sample_repo
    localizer = DeterministicLocalizer(repo_dir)

    tb = f"""Traceback (most recent call last):
  File "{mod_file}", line 11, in write_table
    raise ValueError("Unsupported format")
ValueError: Unsupported format
"""
    candidate = localizer.locate_traceback_frame(tb)
    assert candidate is not None
    assert candidate.file_path == mod_file
    assert candidate.line_number == 11
    assert candidate.score == 1000.0
