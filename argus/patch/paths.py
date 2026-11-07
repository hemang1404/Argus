"""Patch path normalization and single-target enforcement.

Argus currently recovers a single, explicitly selected source file. Unified
diffs can describe several files, renames, creations and deletions; silently
applying one of those hunks to the current target would be a cross-file
misapplication. This module refuses such patches before any text is changed.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Iterable, Optional, Set

from argus.patch.parser import PatchBlock


class PatchTargetError(ValueError):
    """Typed refusal for a patch that does not target exactly the current file."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def normalize_patch_path(raw: Optional[str]) -> Optional[str]:
    """Normalize a unified-diff path to a workspace-relative POSIX path.

    Returns ``None`` for an absent path (search/replace blocks without a hint).
    Raises ``PatchTargetError`` for absolute paths, traversal, NUL bytes and
    device paths such as ``/dev/null``. ``a/`` and ``b/`` prefixes are removed
    only as a leading diff prefix, not as an arbitrary directory component.
    """
    if raw is None:
        return None

    value = raw.strip().replace("\\", "/")
    if not value:
        return None

    if "\x00" in value:
        raise PatchTargetError("INVALID_PATCH_PATH", "Patch path contains a NUL byte.")

    if value in ("/dev/null", "dev/null"):
        raise PatchTargetError(
            "NULL_PATCH_PATH",
            f"Patch path '{raw}' refers to /dev/null (file creation or deletion is unsupported).",
        )

    if value.startswith("/") or value.startswith("//") or _DRIVE_RE.match(value):
        raise PatchTargetError(
            "ABSOLUTE_PATCH_PATH",
            f"Patch path '{raw}' is absolute; only workspace-relative paths are accepted.",
        )

    parts = [part for part in value.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise PatchTargetError(
            "PATCH_PATH_TRAVERSAL",
            f"Patch path '{raw}' contains '..' traversal.",
        )

    # Standard unified-diff prefixes: a/foo.py and b/foo.py.
    if len(parts) >= 2 and parts[0] in ("a", "b"):
        parts = parts[1:]

    if not parts:
        return None

    normalized = PurePosixPath(*parts).as_posix()
    if normalized in ("dev/null",):
        raise PatchTargetError(
            "NULL_PATCH_PATH",
            f"Patch path '{raw}' refers to /dev/null (file creation or deletion is unsupported).",
        )
    return normalized


def validate_patch_blocks(blocks: Iterable[PatchBlock], target_file: str) -> None:
    """Ensure every block targets exactly ``target_file`` and only that file.

    Raises ``PatchTargetError`` with a stable ``code`` on the first violation.
    Blocks without a path hint are accepted because SEARCH/REPLACE output does
    not carry one; the caller has already chosen the single target.
    """
    target_norm = normalize_patch_path(target_file)
    if target_norm is None:
        raise PatchTargetError("INVALID_TARGET", "Recovery target path is empty.")

    distinct: Set[str] = set()

    for index, block in enumerate(blocks, start=1):
        source = normalize_patch_path(getattr(block, "source_path", None))
        destination = normalize_patch_path(block.file_path)

        if source is not None and destination is not None and source != destination:
            raise PatchTargetError(
                "RENAME_UNSUPPORTED",
                f"Block {index} renames '{source}' to '{destination}'; "
                "rename patches are not supported for single-file recovery.",
            )

        for role, path in (("source", source), ("destination", destination)):
            if path is None:
                continue
            if path != target_norm:
                raise PatchTargetError(
                    "PATCH_TARGET_MISMATCH",
                    f"Block {index} {role} path '{path}' does not match the recovery "
                    f"target '{target_norm}'. Refusing cross-file application.",
                )
            distinct.add(path)

    if len(distinct) > 1:
        raise PatchTargetError(
            "MULTI_FILE_PATCH_UNSUPPORTED",
            f"Patch contains multiple target files ({', '.join(sorted(distinct))}); "
            "multi-file recovery is not supported.",
        )
