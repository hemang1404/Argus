"""Frame Slicer & Attention Densification Engine for Argus.

Extracts bounded context windows (±25 lines) around targeted failure frames or symbols,
resolves containing AST node definitions (function/class boundaries), and builds
clean, token-efficient SEARCH/REPLACE prompts for commodity models.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


@dataclass
class FrameSlice:
    """Represents a bounded context slice extracted from a source file."""
    file_path: Path
    relative_path: str
    target_line: int
    start_line: int
    end_line: int
    total_lines: int
    code_slice: str
    numbered_slice: str
    containing_symbol: str = ""

    @property
    def slice_line_count(self) -> int:
        return self.end_line - self.start_line + 1

    @property
    def reduction_percentage(self) -> float:
        if self.total_lines <= 0:
            return 0.0
        return round((1.0 - (self.slice_line_count / self.total_lines)) * 100.0, 1)


class FrameSlicer:
    """Extracts bounded context slices and formats dense prompts."""

    def __init__(self, window_radius: int = 25):
        self.window_radius = window_radius

    def find_containing_node(self, source_code: str, target_line: int) -> Tuple[str, Optional[int], Optional[int]]:
        """Use AST parsing to locate containing function or class and its span."""
        try:
            tree = ast.parse(source_code)
        except Exception:
            return "", None, None

        best_node = None
        best_name = ""
        min_span = float("inf")

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = getattr(node, "lineno", 0)
                end = getattr(node, "end_lineno", start)
                if start <= target_line <= end:
                    span = end - start
                    if span < min_span:
                        min_span = span
                        best_node = node
                        prefix = "class " if isinstance(node, ast.ClassDef) else "def "
                        best_name = prefix + node.name

        if best_node:
            return best_name, getattr(best_node, "lineno", None), getattr(best_node, "end_lineno", None)
        return "", None, None

    def slice_file(
        self,
        file_path: Path,
        repo_root: Path,
        target_line: int,
        expand_to_function: bool = False,
    ) -> FrameSlice:
        """Slice a file around target_line with surrounding context."""
        file_path = Path(file_path).resolve()
        repo_root = Path(repo_root).resolve()
        rel_path = str(file_path.relative_to(repo_root)).replace("\\", "/")

        try:
            raw = file_path.read_bytes()
            # Detect UTF-16 vs UTF-8
            encoding = "utf-16" if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff") else "utf-8"
            source_text = raw.decode(encoding, errors="replace")
        except Exception:
            source_text = ""

        all_lines = source_text.splitlines()
        total_lines = len(all_lines)

        if total_lines == 0:
            return FrameSlice(
                file_path=file_path,
                relative_path=rel_path,
                target_line=target_line,
                start_line=1,
                end_line=1,
                total_lines=0,
                code_slice="",
                numbered_slice="",
            )

        # Ensure target_line is valid
        target_line = max(1, min(target_line, total_lines))

        # Check containing function via AST
        containing_name, fn_start, fn_end = self.find_containing_node(source_text, target_line)

        if expand_to_function and fn_start is not None and fn_end is not None:
            # Expand to containing function body with 5 lines padding
            start_line = max(1, fn_start - 2)
            end_line = min(total_lines, fn_end + 2)
        else:
            # If the file is small (<= 60 lines), include the whole file
            if total_lines <= (self.window_radius * 2 + 10):
                start_line = 1
                end_line = total_lines
            else:
                start_line = max(1, target_line - self.window_radius)
                end_line = min(total_lines, target_line + self.window_radius)

        sliced_lines = all_lines[start_line - 1 : end_line]
        raw_slice = "\n".join(sliced_lines)

        numbered_lines = [f"{start_line + i:4d}: {line}" for i, line in enumerate(sliced_lines)]
        numbered_slice = "\n".join(numbered_lines)

        return FrameSlice(
            file_path=file_path,
            relative_path=rel_path,
            target_line=target_line,
            start_line=start_line,
            end_line=end_line,
            total_lines=total_lines,
            code_slice=raw_slice,
            numbered_slice=numbered_slice,
            containing_symbol=containing_name,
        )

    def build_search_replace_prompt(
        self,
        task_statement: str,
        frame_slice: FrameSlice,
        diagnostic: str = "",
    ) -> str:
        """Build attention-dense SEARCH/REPLACE prompt for commodity models."""
        context_header = f"### File: {frame_slice.relative_path} (Lines {frame_slice.start_line}-{frame_slice.end_line})"
        if frame_slice.containing_symbol:
            context_header += f" inside `{frame_slice.containing_symbol}`"

        diagnostic_block = ""
        if diagnostic:
            diagnostic_block = f"""
## Observed Test Failure / Error Diagnostic:
```text
{diagnostic[:1500]}
```
"""

        return f"""You are fixing a bug in `{frame_slice.relative_path}`.

## Problem Description:
{task_statement}
{diagnostic_block}
## Code Context:
{context_header}

```python
{frame_slice.code_slice}
```

## Instructions:
1. Identify the exact defect in the code context above.
2. Provide a surgical fix using a single SEARCH/REPLACE block.
3. The SEARCH block must EXACTLY match the source lines above (including indentation).
4. Do NOT output unified diff headers (`@@ ... @@`) or line numbers.

Format:
### File: {frame_slice.relative_path}
<<<<<<< SEARCH
[exact lines from code context above]
=======
[corrected replacement lines]
>>>>>>> REPLACE
"""
