"""WP-2 regression tests for patch path normalization and single-target guards."""

from __future__ import annotations

import pytest

from argus.patch.parser import PatchBlock, PatchParser
from argus.patch.paths import PatchTargetError, normalize_patch_path, validate_patch_blocks


def test_normalize_strips_diff_prefixes() -> None:
    assert normalize_patch_path("a/src/mod.py") == "src/mod.py"
    assert normalize_patch_path("b/src/mod.py") == "src/mod.py"
    assert normalize_patch_path("src\\mod.py") == "src/mod.py"
    assert normalize_patch_path(None) is None


@pytest.mark.parametrize(
    "raw,code",
    [
        ("/etc/passwd", "ABSOLUTE_PATCH_PATH"),
        ("C:\\Windows\\system.py", "ABSOLUTE_PATCH_PATH"),
        ("../outside.py", "PATCH_PATH_TRAVERSAL"),
        ("src/../../outside.py", "PATCH_PATH_TRAVERSAL"),
        ("/dev/null", "NULL_PATCH_PATH"),
        ("a/\x00mod.py", "INVALID_PATCH_PATH"),
    ],
)
def test_normalize_rejects_unsafe_paths(raw: str, code: str) -> None:
    with pytest.raises(PatchTargetError) as exc:
        normalize_patch_path(raw)
    assert exc.value.code == code


def test_legitimate_single_file_passes() -> None:
    blocks = [
        PatchBlock(search_text="x = 1", replace_text="x = 2", file_path="b/src/mod.py"),
        PatchBlock(search_text="y = 1", replace_text="y = 2", file_path="a/src/mod.py"),
    ]
    validate_patch_blocks(blocks, "src/mod.py")  # must not raise


def test_cross_file_hunk_is_refused_even_when_code_would_match() -> None:
    text = (
        "diff --git a/one.py b/one.py\n"
        "--- a/one.py\n+++ b/one.py\n"
        "@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n"
        "diff --git a/two.py b/two.py\n"
        "--- a/two.py\n+++ b/two.py\n"
        "@@ -1,1 +1,1 @@\n-x = 1\n+x = 3\n"
    )
    blocks = PatchParser.parse_unified_diff(text)
    assert len(blocks) == 2
    with pytest.raises(PatchTargetError) as exc:
        validate_patch_blocks(blocks, "one.py")
    assert exc.value.code in ("PATCH_TARGET_MISMATCH", "MULTI_FILE_PATCH_UNSUPPORTED")


def test_multi_file_patch_is_refused_for_single_target() -> None:
    blocks = [
        PatchBlock(search_text="x = 1", replace_text="x = 2", file_path="one.py"),
        PatchBlock(search_text="y = 1", replace_text="y = 2", file_path="two.py"),
    ]
    with pytest.raises(PatchTargetError) as exc:
        validate_patch_blocks(blocks, "one.py")
    assert exc.value.code == "PATCH_TARGET_MISMATCH"


def test_rename_is_refused() -> None:
    block = PatchBlock(
        search_text="x = 1",
        replace_text="x = 2",
        file_path="b/new.py",
        source_path="a/old.py",
    )
    with pytest.raises(PatchTargetError) as exc:
        validate_patch_blocks([block], "new.py")
    assert exc.value.code == "RENAME_UNSUPPORTED"


def test_creation_through_dev_null_is_refused() -> None:
    text = (
        "diff --git a/new.py b/new.py\n"
        "--- /dev/null\n+++ b/new.py\n"
        "@@ -0,0 +1,1 @@\n+x = 1\n"
    )
    blocks = PatchParser.parse_unified_diff(text)
    assert len(blocks) == 1
    with pytest.raises(PatchTargetError) as exc:
        validate_patch_blocks(blocks, "new.py")
    assert exc.value.code == "NULL_PATCH_PATH"


def test_deletion_through_dev_null_is_refused() -> None:
    text = (
        "diff --git a/old.py b/old.py\n"
        "--- a/old.py\n+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n-x = 1\n"
    )
    blocks = PatchParser.parse_unified_diff(text)
    assert len(blocks) == 1
    with pytest.raises(PatchTargetError) as exc:
        validate_patch_blocks(blocks, "old.py")
    assert exc.value.code == "NULL_PATCH_PATH"


def test_pathless_search_replace_blocks_are_allowed() -> None:
    validate_patch_blocks(
        [PatchBlock(search_text="old", replace_text="new")],
        "src/mod.py",
    )
