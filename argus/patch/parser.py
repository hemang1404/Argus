"""Patch parsing utilities for LLM-generated code modifications."""

from __future__ import annotations

import re
from dataclasses import dataclass
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


class PatchParser:
    """Parses various LLM patch formats into structured PatchBlock objects."""

    # Matches search/replace blocks with variable marker lengths:
    # <<<<<<< SEARCH [path]
    # ...
    # =======
    # ...
    # >>>>>>> REPLACE
    SEARCH_REPLACE_PATTERN = re.compile(
        r"<{3,}[ \t]*SEARCH(?:[ \t]+([^\r\n]+))?\r?\n(.*?)\r?\n={3,}\r?\n(.*?)\r?\n>{3,}[ \t]*REPLACE",
        re.DOTALL,
    )

    # Matches standard unified diff hunks:
    # @@ -start,len +start,len @@
    DIFF_HUNK_PATTERN = re.compile(
        r"@@\s*-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s*@@[^\r\n]*\r?\n(.*?)(?=(?:\r?\n@@|\r?\n---|\r?\n\+\+\+|\Z))",
        re.DOTALL,
    )

    @staticmethod
    def strip_line_annotations(text: str) -> str:
        """Strip accidental line-number annotations like '   2 | ' emitted by LLMs."""
        pattern = re.compile(r"^[ \t]*\d+[ \t]*\|[ \t]?", re.MULTILINE)
        if pattern.search(text):
            lines = text.splitlines(keepends=True)
            return "".join(pattern.sub("", l) for l in lines)
        return text

    @classmethod
    def parse_search_replace(cls, text: str, default_path: Optional[str] = None) -> List[PatchBlock]:
        """Extract all SEARCH/REPLACE blocks from LLM output."""
        blocks: List[PatchBlock] = []
        for match in cls.SEARCH_REPLACE_PATTERN.finditer(text):
            path_hint = match.group(1)
            search_content = cls.strip_line_annotations(match.group(2))
            replace_content = cls.strip_line_annotations(match.group(3))
            
            cleaned_path = path_hint.strip() if path_hint else default_path
            if not cleaned_path:
                # Look backwards in preceding text for ### File: path
                pre_text = text[:match.start()]
                m = re.findall(r"(?:###|\*\*|##)?\s*(?:File|Path|Filename):\s*`?([^\n`\r]+)`?", pre_text, re.IGNORECASE)
                if m:
                    cleaned_path = m[-1].strip().replace("\\", "/")

            blocks.append(
                PatchBlock(
                    search_text=search_content,
                    replace_text=replace_content,
                    file_path=cleaned_path,
                )
            )
        return blocks

    @classmethod
    def parse_unified_diff(cls, text: str, default_path: Optional[str] = None) -> List[PatchBlock]:
        """Convert unified diff hunks into search/replace blocks with multi-file support."""
        blocks: List[PatchBlock] = []
        # Prefer diff --git boundaries so both --- and +++ headers stay in the
        # same section. Plain unified diffs fall back to --- boundaries.
        if re.search(r"(?m)^diff --git\s+", text):
            file_sections = re.split(r"(?=(?:^|\r?\n)diff --git\s+)", text)
        else:
            file_sections = re.split(r"(?=(?:^|\r?\n)---\s+)", text)

        for section in file_sections:
            if not section.strip():
                continue
            old_match = re.search(r"^---\s+(.*?)(?:\t|\r?\n)", section, re.MULTILINE)
            new_match = re.search(r"^\+\+\+\s+(.*?)(?:\t|\r?\n)", section, re.MULTILINE)
            # Keep /dev/null verbatim so creation and deletion patches are
            # refused by the path guard instead of appearing path-less. Strip
            # only the conventional a/ and b/ diff prefixes for compatibility
            # with existing block consumers.
            def _strip_diff_prefix(value: str) -> str:
                value = value.strip()
                if value in ("/dev/null", "dev/null"):
                    return value
                if value.startswith(("a/", "b/")):
                    return value[2:]
                return value

            section_path = _strip_diff_prefix(new_match.group(1)) if new_match else None
            source_path = _strip_diff_prefix(old_match.group(1)) if old_match else None
            if section_path is None and source_path is None:
                section_path = default_path

            for match in cls.DIFF_HUNK_PATTERN.finditer(section):
                hunk_body = match.group(1)
                search_lines = []
                replace_lines = []

                for line in hunk_body.splitlines():
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
        """Auto-detect format and parse patches from response text."""
        # 1. First try Search/Replace format (most reliable)
        sr_blocks = cls.parse_search_replace(response_text, default_path=default_path)
        if sr_blocks:
            return sr_blocks

        # 2. Try unified diff format
        if "@@" in response_text or response_text.startswith("--- ") or "\n--- " in response_text:
            diff_blocks = cls.parse_unified_diff(response_text, default_path=default_path)
            if diff_blocks:
                return diff_blocks

        # 3. If raw markdown code fence contains SEARCH/REPLACE
        fence_match = re.search(r"```(?:python|diff)?\r?\n(.*?)\r?\n```", response_text, re.DOTALL)
        if fence_match:
            inner_text = fence_match.group(1)
            inner_sr = cls.parse_search_replace(inner_text, default_path=default_path)
            if inner_sr:
                return inner_sr
            inner_diff = cls.parse_unified_diff(inner_text, default_path=default_path)
            if inner_diff:
                return inner_diff

        return []
