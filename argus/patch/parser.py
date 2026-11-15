"""Patch parsing utilities for LLM-generated code modifications."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class PatchBlock:
    """Represents a discrete search/replace modification block."""
    search_text: str
    replace_text: str
    file_path: Optional[str] = None
    # Unified diffs carry both sides of a file header. ``source_path`` is kept
    # so renames and deletions can be rejected instead of being mistaken for a
    # patch against ``file_path`` alone.
    source_path: Optional[str] = None


@dataclass
class ParseOutcome:
    """Blocks plus structural problems found while parsing SEARCH/REPLACE text."""
    blocks: List[PatchBlock] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class PatchParser:
    """Parses various LLM patch formats into structured PatchBlock objects."""

    # Marker lines of the SEARCH/REPLACE format (variable marker lengths):
    # <<<<<<< SEARCH [path]
    # ...
    # =======
    # ...
    # >>>>>>> REPLACE
    _START_RE = re.compile(r"^[ \t]*<{3,}[ \t]*SEARCH(?:[ \t]+(\S.*?))?[ \t]*$")
    _SEP_RE = re.compile(r"^[ \t]*={3,}[ \t]*$")
    _END_RE = re.compile(r"^[ \t]*>{3,}[ \t]*REPLACE\b.*$")

    _HUNK_HEADER_RE = re.compile(r"^@@\s*-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s*@@")
    _ANNOTATION_RE = re.compile(r"^[ \t]*(\d+)[ \t]*\|[ \t]?")

    @classmethod
    def strip_line_annotations(cls, text: str) -> str:
        """Strip accidental line-number annotations like '   2 | ' emitted by LLMs.

        Only applied when EVERY non-blank line carries an annotation (and the
        numbers count up by one), so legitimate code that happens to contain
        ``1 | b`` on a line is never rewritten.
        """
        lines = text.splitlines(keepends=True)
        numbers: List[int] = []
        for line in lines:
            if not line.strip():
                continue
            m = cls._ANNOTATION_RE.match(line)
            if not m:
                return text
            numbers.append(int(m.group(1)))
        if not numbers:
            return text
        if any(b - a != 1 for a, b in zip(numbers, numbers[1:])):
            return text
        return "".join(cls._ANNOTATION_RE.sub("", line, count=1) for line in lines)

    @classmethod
    def parse_search_replace_detailed(cls, text: str, default_path: Optional[str] = None) -> ParseOutcome:
        """Parse SEARCH/REPLACE blocks and report malformed or truncated ones.

        A block that is cut off (a new ``<<<<<<< SEARCH`` or the end of the text
        arrives before its ``>>>>>>> REPLACE``) is reported in ``errors`` and is
        never merged into, or allowed to swallow, a neighbouring block. An empty
        REPLACE section is valid (pure deletion).
        """
        outcome = ParseOutcome()
        lines = text.split("\n")
        state = "out"  # out | search | replace
        path_hint: Optional[str] = None
        block_start = 0
        search_buf: List[str] = []
        replace_buf: List[str] = []
        before: List[str] = []

        def finish() -> None:
            cleaned_path = path_hint.strip() if path_hint else default_path
            if not cleaned_path:
                # Look backwards in preceding text for ### File: path
                found = re.findall(
                    r"(?:###|\*\*|##)?\s*(?:File|Path|Filename):\s*`?([^\n`\r]+)`?",
                    "\n".join(before),
                    re.IGNORECASE,
                )
                if found:
                    cleaned_path = found[-1].strip().replace("\\", "/")
            outcome.blocks.append(
                PatchBlock(
                    search_text=cls.strip_line_annotations("\n".join(search_buf)),
                    replace_text=cls.strip_line_annotations("\n".join(replace_buf)),
                    file_path=cleaned_path,
                )
            )

        for idx, raw in enumerate(lines):
            line = raw.rstrip("\r")
            start = cls._START_RE.match(line)
            if state == "out":
                if start:
                    state = "search"
                    path_hint = start.group(1)
                    block_start = idx
                    before = lines[:idx]
                    search_buf, replace_buf = [], []
                continue
            if start:
                outcome.errors.append(
                    f"Truncated SEARCH/REPLACE block starting at line {block_start + 1}: "
                    f"a new SEARCH marker appeared at line {idx + 1} before its REPLACE terminator."
                )
                state = "search"
                path_hint = start.group(1)
                block_start = idx
                before = lines[:idx]
                search_buf, replace_buf = [], []
                continue
            if state == "search":
                if cls._SEP_RE.match(line):
                    state = "replace"
                elif cls._END_RE.match(line):
                    outcome.errors.append(
                        f"Malformed SEARCH/REPLACE block at line {block_start + 1}: "
                        "REPLACE terminator without a '=======' separator."
                    )
                    state = "out"
                else:
                    search_buf.append(line)
            else:  # replace
                if cls._END_RE.match(line):
                    finish()
                    state = "out"
                else:
                    replace_buf.append(line)

        if state != "out":
            outcome.errors.append(
                f"Truncated SEARCH/REPLACE block starting at line {block_start + 1}: "
                "end of response reached before the REPLACE terminator."
            )
        return outcome

    @classmethod
    def parse_search_replace(cls, text: str, default_path: Optional[str] = None) -> List[PatchBlock]:
        """Extract SEARCH/REPLACE blocks. Returns [] if any block is malformed or truncated.

        Applying only the well-formed part of a damaged response could yield a
        half-applied edit, so a damaged response produces no blocks. Use
        ``parse_search_replace_detailed`` to learn why.
        """
        outcome = cls.parse_search_replace_detailed(text, default_path=default_path)
        return [] if outcome.errors else outcome.blocks

    @classmethod
    def _split_diff_hunks(cls, lines: List[str]) -> List[List[str]]:
        """Group a file section's lines into hunk bodies.

        A hunk ends at the next ``@@`` header, a ``diff --git`` line, a real
        ``---``/``+++`` file-header pair, or the end of the section. A removed
        line that merely starts with ``--`` (``--- x``) is body, not a header.
        """
        hunks: List[List[str]] = []
        current: Optional[List[str]] = None
        for i, line in enumerate(lines):
            if cls._HUNK_HEADER_RE.match(line):
                current = []
                hunks.append(current)
                continue
            if current is None:
                continue
            is_file_header = (
                line.startswith("--- ")
                and i + 1 < len(lines)
                and lines[i + 1].startswith("+++ ")
            )
            if is_file_header or line.startswith("diff --git "):
                current = None
                continue
            current.append(line)
        return hunks

    @classmethod
    def parse_unified_diff(cls, text: str, default_path: Optional[str] = None) -> List[PatchBlock]:
        """Convert unified diff hunks into search/replace blocks with multi-file support."""
        blocks: List[PatchBlock] = []
        # Prefer diff --git boundaries so both --- and +++ headers stay in the
        # same section. Plain unified diffs split on a ``---`` line that is
        # immediately followed by a ``+++`` line (a removed ``-- x`` line is not
        # a file header).
        if re.search(r"(?m)^diff --git\s+", text):
            file_sections = re.split(r"(?m)^(?=diff --git\s+)", text)
        else:
            file_sections = re.split(r"(?m)^(?=--- [^\n]*\n\+\+\+ )", text)

        def _strip_diff_prefix(value: str) -> str:
            # Keep /dev/null verbatim so creation and deletion patches are
            # refused by the path guard instead of appearing path-less. Strip
            # only the conventional a/ and b/ diff prefixes.
            value = value.strip()
            if value in ("/dev/null", "dev/null"):
                return value
            if value.startswith(("a/", "b/")):
                return value[2:]
            return value

        for section in file_sections:
            if not section.strip():
                continue
            old_match = re.search(r"^---\s+(.*?)(?:\t|\r?\n)", section, re.MULTILINE)
            new_match = re.search(r"^\+\+\+\s+(.*?)(?:\t|\r?\n)", section, re.MULTILINE)
            section_path = _strip_diff_prefix(new_match.group(1)) if new_match else None
            source_path = _strip_diff_prefix(old_match.group(1)) if old_match else None
            if section_path is None and source_path is None:
                section_path = default_path

            section_lines = [ln.rstrip("\r") for ln in section.split("\n")]
            for hunk_body in cls._split_diff_hunks(section_lines):
                # Trailing empty lines are section padding, not context.
                while hunk_body and hunk_body[-1] == "":
                    hunk_body.pop()
                search_lines = []
                replace_lines = []

                for line in hunk_body:
                    if line.startswith("-"):
                        search_lines.append(line[1:])
                    elif line.startswith("+"):
                        replace_lines.append(line[1:])
                    elif line.startswith(" "):
                        search_lines.append(line[1:])
                        replace_lines.append(line[1:])
                    elif line.startswith("\\"):
                        # e.g., \ No newline at end of file
                        continue
                    else:
                        search_lines.append(line)
                        replace_lines.append(line)

                search_block = "\n".join(search_lines)
                replace_block = "\n".join(replace_lines)
                if search_block or replace_block:
                    blocks.append(
                        PatchBlock(
                            search_text=search_block,
                            replace_text=replace_block,
                            file_path=section_path,
                            source_path=source_path,
                        )
                    )

        return blocks

    @classmethod
    def parse(cls, response_text: str, default_path: Optional[str] = None) -> List[PatchBlock]:
        """Auto-detect format and parse patches from response text.

        A damaged SEARCH/REPLACE response (truncated or malformed block) yields
        no blocks rather than a partially-applicable patch.
        """
        # 1. First try Search/Replace format (most reliable)
        sr = cls.parse_search_replace_detailed(response_text, default_path=default_path)
        if sr.errors:
            return []
        if sr.blocks:
            return sr.blocks

        # 2. Try unified diff format
        if "@@" in response_text or response_text.startswith("--- ") or "\n--- " in response_text:
            diff_blocks = cls.parse_unified_diff(response_text, default_path=default_path)
            if diff_blocks:
                return diff_blocks

        # 3. If raw markdown code fence contains SEARCH/REPLACE
        fence_match = re.search(r"```(?:python|diff)?\r?\n(.*?)\r?\n```", response_text, re.DOTALL)
        if fence_match:
            inner_text = fence_match.group(1)
            inner = cls.parse_search_replace_detailed(inner_text, default_path=default_path)
            if inner.errors:
                return []
            if inner.blocks:
                return inner.blocks
            inner_diff = cls.parse_unified_diff(inner_text, default_path=default_path)
            if inner_diff:
                return inner_diff

        return []
