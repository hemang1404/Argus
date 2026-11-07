"""Unit tests for Argus Deterministic Hybrid Pipeline (Localizer and FrameSlicer)."""

import tempfile
from pathlib import Path
import pytest

from argus.localization.localizer import DeterministicLocalizer, LocalizationCandidate
from argus.runtime.frame_slicer import FrameSlicer, FrameSlice


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


def test_frame_slicer_bounds_and_ast(sample_repo):
    repo_dir, mod_file = sample_repo
    slicer = FrameSlicer(window_radius=5)

    slice_res = slicer.slice_file(mod_file, repo_dir, target_line=11)
    assert slice_res.total_lines > 0
    assert "def write_table" in slice_res.code_slice
    assert "write_table" in slice_res.containing_symbol

    # Test prompt generation
    prompt = slicer.build_search_replace_prompt(
        task_statement="Support additional formats",
        frame_slice=slice_res,
        diagnostic="ValueError: Unsupported format"
    )
    assert "<<<<<<< SEARCH" in prompt
    assert ">>>>>>> REPLACE" in prompt
    assert "def write_table" in prompt
    assert "### File: mypackage/handler.py" in prompt
