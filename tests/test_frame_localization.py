"""Unit tests for frame localization and targeted context slicing in RecoveryController."""

from __future__ import annotations

import pytest
from argus.runtime.controller import RecoveryController, extract_context_frame


def test_extract_frame_small_file_returns_full_code():
    """Verify that small files (<=60 lines) are returned in full without truncation."""
    small_code = "\n".join(f"def func_{i}(): pass" for i in range(20))
    traceback = 'File "test_file.py", line 10, in func_9'

    annotated, start, end, target = extract_context_frame(
        code=small_code,
        file_path="test_file.py",
        error_feedback=traceback,
        window_radius=25,
    )

    assert start == 1
    assert end == 20
    assert target is None  # Small files keep full context
    assert "   1 | def func_0(): pass" in annotated
    assert "  20 | def func_19(): pass" in annotated


def test_extract_frame_standard_python_traceback():
    """Verify standard Python traceback 'File "...", line X' extracts the localized window."""
    code_lines = [f"# Line {i+1}\nx = {i+1}" for i in range(200)]
    large_code = "\n".join(code_lines)
    traceback = (
        'Traceback (most recent call last):\n'
        '  File "module/runner.py", line 40, in run\n'
        '  File "core/calculator.py", line 120, in calculate\n'
        '    assert result == 42\n'
        'AssertionError: expected 42\n'
    )

    annotated, start, end, target = extract_context_frame(
        code=large_code,
        file_path="core/calculator.py",
        error_feedback=traceback,
        window_radius=15,
    )

    assert target == 120
    assert start == 120 - 15  # 105
    assert end == 120 + 15    # 135
    assert " 105 | " in annotated
    assert " 120 | " in annotated
    assert " 135 | " in annotated
    assert " 100 | " not in annotated
    assert " 140 | " not in annotated


def test_extract_frame_pytest_colon_syntax():
    """Verify pytest-style 'file.py:142: in test_func' extracts the localized window."""
    code_lines = [f"line_{i+1} = {i+1}" for i in range(150)]
    large_code = "\n".join(code_lines)
    pytest_traceback = (
        "tests/test_engine.py:30: in test_foo\n"
        "    engine.execute()\n"
        "core/engine.py:80: in execute\n"
        ">       raise ValueError('invalid state')\n"
        "E       ValueError: invalid state\n"
    )

    annotated, start, end, target = extract_context_frame(
        code=large_code,
        file_path="core/engine.py",
        error_feedback=pytest_traceback,
        window_radius=20,
    )

    assert target == 80
    assert start == 60
    assert end == 100
    assert "  60 | line_60 = 60" in annotated
    assert "  80 | line_80 = 80" in annotated
    assert " 100 | line_100 = 100" in annotated


def test_extract_frame_picks_deepest_frame():
    """Verify that when multiple frames mention the same file, the innermost crash site is selected."""
    code_lines = [f"# L{i+1}" for i in range(300)]
    large_code = "\n".join(code_lines)
    multi_frame_traceback = (
        'Traceback (most recent call last):\n'
        '  File "utils.py", line 50, in outer_func\n'
        '    helper()\n'
        '  File "utils.py", line 220, in helper\n'
        '    crash()\n'
        'ZeroDivisionError: division by zero\n'
    )

    annotated, start, end, target = extract_context_frame(
        code=large_code,
        file_path="utils.py",
        error_feedback=multi_frame_traceback,
        window_radius=10,
    )

    assert target == 220
    assert start == 210
    assert end == 230


def test_extract_frame_fallback_when_no_frame_matches():
    """Verify that when error traceback mentions files other than the target and no symbol matches, an explicit localization failure (0, 0) is returned."""
    code_lines = [f"row_{i+1} = True" for i in range(250)]
    large_code = "\n".join(code_lines)
    unrelated_traceback = (
        'Traceback (most recent call last):\n'
        '  File "external_lib/core.py", line 99, in run\n'
        'ImportError: missing dependency\n'
    )

    annotated, start, end, target = extract_context_frame(
        code=large_code,
        file_path="my_module.py",
        error_feedback=unrelated_traceback,
        window_radius=25,
    )

    assert target is None
    assert start == 0
    assert end == 0
    assert annotated == ""


def test_build_prompt_includes_localized_context_and_feedback():
    """Verify RecoveryController._build_prompt generates the localized slice header and instructions."""
    controller = RecoveryController()
    code_lines = [f"val_{i+1} = {i+1}" for i in range(120)]
    code = "\n".join(code_lines)
    tb = 'File "src/logic.py", line 50, in compute\nAssertionError'

    prompt = controller._build_prompt(
        file_path="src/logic.py",
        code=code,
        test_command="pytest tests/test_logic.py",
        error_feedback=tb,
        attempt=1,
    )

    assert "#### Localized Source Code Context:" in prompt
    assert "Showing lines 25-75 of 120 in `src/logic.py` (focused around failure frame line 50):" in prompt
    assert "<<<<<<< SEARCH" in prompt
    assert ">>>>>>> REPLACE" in prompt
    assert "val_50 = 50" in prompt


def test_build_prompt_preserves_root_bug_across_attempts():
    """Verify that multi-attempt prompts present both the root test failure and intermediate feedback."""
    controller = RecoveryController()
    code = "def add(a, b):\n    return a - b\n"
    root_bug = "AssertionError: assert add(2, 3) == 5 (got -1)"
    syntax_error_feedback = "Patch application failed: AST syntax error: SyntaxError at line 2 col 10"

    prompt = controller._build_prompt(
        file_path="calc.py",
        code=code,
        test_command="pytest tests/test_calc.py",
        error_feedback=syntax_error_feedback,
        attempt=2,
        original_failure=root_bug,
    )

    assert "#### Primary Test Failure (Root Bug):" in prompt
    assert "assert add(2, 3) == 5 (got -1)" in prompt
    assert "#### Feedback / Diagnostic from Attempt 1:" in prompt
    assert "AST syntax error: SyntaxError at line 2 col 10" in prompt
