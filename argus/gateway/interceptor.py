"""Streaming interceptor: Passes prose through immediately, buffers code blocks, heals diffs via 5-tier patch engine."""

from __future__ import annotations

import re
from pathlib import Path
from typing import AsyncGenerator, Optional
from argus.patch.fuzzy_engine import ResilientPatchEngine, PatchResult
from argus.patch.parser import PatchBlock, PatchParser


class StreamPatchHealer:
    """Intercepts and heals streaming patch blocks against local workspace files."""

    def __init__(self, workspace: Path | str = ".", enabled: bool = True) -> None:
        self.workspace = Path(workspace).resolve()
        self.enabled = enabled
        self.patch_engine = ResilientPatchEngine(enforce_ast=True)

    def heal_block(self, block_text: str) -> str:
        """Attempt to heal a search/replace or diff block against the workspace."""
        if not self.enabled:
            return block_text

        patches = PatchParser.parse(block_text)
        if not patches:
            return block_text

        patch = patches[0]
        target_path: Optional[Path] = None

        # 1. Resolve target file path
        if patch.file_path:
            cand = (self.workspace / patch.file_path).resolve()
            if cand.is_file() and str(cand).startswith(str(self.workspace)):
                target_path = cand
            else:
                # Try basename match across workspace
                fname = Path(patch.file_path).name
                matches = list(self.workspace.glob(f"**/{fname}"))
                if len(matches) == 1 and matches[0].is_file():
                    target_path = matches[0]

        if not target_path or not target_path.is_file():
            # Check if block_text mentions any file in workspace
            candidates = []
            for ext in ("*.py", "*.js", "*.ts", "*.json", "*.html", "*.css"):
                for f in self.workspace.glob(f"**/{ext}"):
                    if f.is_file() and f.name in block_text:
                        candidates.append(f)
            if len(candidates) == 1:
                target_path = candidates[0]
            elif not candidates:
                # If workspace has exactly 1 source file, use it
                all_srcs = [f for f in self.workspace.glob("*.*") if f.is_file() and f.suffix in (".py", ".js", ".ts")]
                if len(all_srcs) == 1:
                    target_path = all_srcs[0]

        if not target_path or not target_path.is_file():
            # Target file cannot be unambiguously resolved, pass through as-is
            return block_text

        try:
            original_content = target_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return block_text

        # 2. Dry-run strict match
        if patch.search_text and patch.search_text in original_content:
            # Strict match already works, release untouched!
            return block_text

        # 3. Fuzzy healing using ResilientPatchEngine
        res: PatchResult = self.patch_engine.apply_patch(
            source_code=original_content,
            patch=patch,
            filename=str(target_path.relative_to(self.workspace)),
        )

        if res.success and res.matched_search_block and res.tier_used and res.tier_used > 1:
            # Healing recovered the edit! Re-synthesize a healed SEARCH/REPLACE block
            # with the exact matched lines from the real file
            healed = (
                f"<<<<<<< SEARCH\n"
                f"{res.matched_search_block}\n"
                f"=======\n"
                f"{patch.replace_text}\n"
                f">>>>>>> REPLACE"
            )
            return healed

        return block_text


class StreamInterceptor:
    """Stateful stream processor buffering code/diff blocks for healing while passing prose immediately."""

    def __init__(
        self,
        healer: StreamPatchHealer,
        pass_through: bool = False,
        max_buffer_chars: int = 32768,
    ) -> None:
        self.healer = healer
        self.pass_through = pass_through or (not healer.enabled)
        self.max_buffer_chars = max_buffer_chars
        self.buffer = ""
        self.in_patch_block = False

    def process_chunk(self, chunk_text: str) -> str:
        """Process an incremental text delta from the upstream LLM stream."""
        if self.pass_through:
            return chunk_text

        self.buffer += chunk_text

        # Check if we should enter patch buffering mode
        if not self.in_patch_block:
            if "<<<<<<< SEARCH" in self.buffer:
                self.in_patch_block = True
                parts = self.buffer.split("<<<<<<< SEARCH", 1)
                prose = parts[0]
                self.buffer = "<<<<<<< SEARCH" + parts[1]
                return prose
            else:
                # Safe prose emission
                out = self.buffer
                self.buffer = ""
                return out

        # In patch buffering mode: wait for closing terminator
        if ">>>>>>> REPLACE" in self.buffer:
            parts = self.buffer.split(">>>>>>> REPLACE", 1)
            full_patch = parts[0] + ">>>>>>> REPLACE"
            remainder = parts[1] if len(parts) > 1 else ""

            healed_patch = self.healer.heal_block(full_patch)
            self.buffer = remainder
            self.in_patch_block = False
            return healed_patch

        # Safety cap: avoid indefinite buffering stall if model generates huge block without terminator
        if len(self.buffer) > self.max_buffer_chars:
            out = self.buffer
            self.buffer = ""
            self.in_patch_block = False
            return out

        return ""

    def flush(self) -> str:
        """Flush any remaining buffered text when stream closes."""
        if not self.buffer:
            return ""
        if self.pass_through:
            out = self.buffer
            self.buffer = ""
            return out
        if self.in_patch_block:
            healed = self.healer.heal_block(self.buffer)
            self.buffer = ""
            return healed
        out = self.buffer
        self.buffer = ""
        return out

