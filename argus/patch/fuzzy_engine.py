"""5-Tier Resilient Fuzzy Patch Engine with Universal AST Validation and Strict Ambiguity Refusal."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

from argus.patch.ast_checker import ASTChecker, ASTValidationResult
from argus.patch.parser import PatchBlock


class RefusalReason(str, Enum):
    NONE = "NONE"
    REFUSED_NO_MATCH = "REFUSED_NO_MATCH"
    REFUSED_AMBIGUOUS_MULTIPLE_EXACT_MATCHES = "REFUSED_AMBIGUOUS_MULTIPLE_EXACT_MATCHES"
    REFUSED_AMBIGUOUS_FUZZY_MATCH = "REFUSED_AMBIGUOUS_FUZZY_MATCH"
    REFUSED_AMBIGUOUS_ANCHOR_MATCH = "REFUSED_AMBIGUOUS_ANCHOR_MATCH"
    REFUSED_SYNTAX_ERROR = "REFUSED_SYNTAX_ERROR"
    REFUSED_EMPTY_PATCH = "REFUSED_EMPTY_PATCH"
    REFUSED_UNSUPPORTED_LANGUAGE = "REFUSED_UNSUPPORTED_LANGUAGE"
    REFUSED_ANCHOR_INTERIOR_MISMATCH = "REFUSED_ANCHOR_INTERIOR_MISMATCH"


@dataclass
class PatchResult:
    """Outcome of attempting to apply a patch to source code."""
    success: bool
    tier_used: Optional[int] = None
    refusal_reason: RefusalReason = RefusalReason.NONE
    patched_code: Optional[str] = None
    matched_search_block: Optional[str] = None
    ast_result: Optional[ASTValidationResult] = None
    details: str = ""
    # Provenance for observability. matched_file is filled by the controller,
    # which owns the single-target workspace context.
    matched_file: Optional[str] = None
    strategy: str = ""
    confidence: Optional[float] = None


class ResilientPatchEngine:
    """5-Tier Fuzzy Diff Reconciliation Engine."""

    def __init__(
        self,
        similarity_threshold: float = 0.82,
        ambiguity_margin: float = 0.10,
        enforce_ast: bool = True,
        enable_tier4: bool = False,
    ) -> None:
        self.similarity_threshold = similarity_threshold
        self.ambiguity_margin = ambiguity_margin
        self.enforce_ast = enforce_ast
        # Tier 4 has narrow demonstrated positive coverage but no successful
        # cases in the adversarial corpus. It is experimental and must be
        # explicitly enabled; the default recovery path stops at Tier 3.
        self.enable_tier4 = enable_tier4
        # Tier 4 replaces an entire anchored span, so it requires a much higher
        # interior similarity than the sliding-window tier. This is deliberately
        # conservative: a false positive here can rewrite unrelated code.
        self.tier4_min_similarity = 0.85
        # Tier 3 only needs to reject an obviously unrelated interior when the
        # boundary anchors match; normal drift (a renamed variable) must remain
        # recoverable, so this threshold is deliberately lower than Tier 4's.
        self.tier3_anchor_interior_min_similarity = 0.60

    def apply_patch(
        self,
        source_code: str,
        patch: PatchBlock,
        filename: str = "<target>",
    ) -> PatchResult:
        """Attempt to apply a single PatchBlock across the 5 tiers with AST verification."""
        search = patch.search_text
        replace = patch.replace_text

        if not search.strip():
            return PatchResult(
                success=False,
                refusal_reason=RefusalReason.REFUSED_EMPTY_PATCH,
                strategy="none",
                details="Search block is empty.",
            )

        # Normalize line endings to \n for consistent reconciliation
        src_normalized = source_code.replace("\r\n", "\n")
        search_normalized = search.replace("\r\n", "\n")
        replace_normalized = replace.replace("\r\n", "\n")

        # Tier 1: Exact Match
        t1_res = self._try_tier1_exact(src_normalized, search_normalized, replace_normalized)
        if t1_res:
            return self._verify_and_finalize(t1_res, filename)

        # Tier 2: Normalized Whitespace / Indentation
        t2_res = self._try_tier2_normalized(src_normalized, search_normalized, replace_normalized)
        if t2_res:
            return self._verify_and_finalize(t2_res, filename)

        # Tier 3: Levenshtein Fuzzy Sliding Window
        t3_res = self._try_tier3_fuzzy(src_normalized, search_normalized, replace_normalized)
        if t3_res:
            return self._verify_and_finalize(t3_res, filename)

        # Tier 4: Block Anchor Recovery (experimental, opt-in only).
        if self.enable_tier4:
            t4_res = self._try_tier4_anchors(src_normalized, search_normalized, replace_normalized)
            if t4_res:
                return self._verify_and_finalize(t4_res, filename)

        # Tier 5: Safe Refusal
        return PatchResult(
            success=False,
            tier_used=5,
            refusal_reason=RefusalReason.REFUSED_NO_MATCH,
            strategy="none",
            details=f"No matching region found for search block (checked Tiers 1-4).",
        )

    def _verify_and_finalize(self, candidate_result: PatchResult, filename: str) -> PatchResult:
        """Enforce universal full-file AST check on candidate code before allowing success."""
        if not candidate_result.success:
            return candidate_result

        assert candidate_result.patched_code is not None

        # If file is a python file, parse the entire module
        is_python = filename.endswith(".py") or filename in ("<target>", "<synthesized>")
        if not is_python and self.enforce_ast:
            return PatchResult(
                success=False,
                tier_used=candidate_result.tier_used,
                refusal_reason=RefusalReason.REFUSED_UNSUPPORTED_LANGUAGE,
                strategy=candidate_result.strategy,
                confidence=candidate_result.confidence,
                details=(
                    f"Refusing to apply to non-Python target '{filename}': no reliable "
                    "language-specific validator is available, so AST gating cannot be claimed."
                ),
            )

        if self.enforce_ast and is_python:
            ast_res = ASTChecker.validate_python_code(candidate_result.patched_code, filename=filename)
            candidate_result.ast_result = ast_res
            if not ast_res.is_valid:
                return PatchResult(
                    success=False,
                    tier_used=candidate_result.tier_used,
                    refusal_reason=RefusalReason.REFUSED_SYNTAX_ERROR,
                    ast_result=ast_res,
                    strategy=candidate_result.strategy,
                    confidence=candidate_result.confidence,
                    details=f"AST syntax error in synthesized code: {ast_res.formatted_error()}",
                )

        return candidate_result

    # -------------------------------------------------------------------------
    # Tier Implementations
    # -------------------------------------------------------------------------

    def _try_tier1_exact(self, source: str, search: str, replace: str) -> Optional[PatchResult]:
        """Tier 1: Character-exact match with ambiguity detection."""
        occurrences = source.count(search)
        if occurrences == 1:
            patched = source.replace(search, replace, 1)
            return PatchResult(
                success=True,
                tier_used=1,
                patched_code=patched,
                matched_search_block=search,
                strategy="exact",
                confidence=1.0,
                details="Tier 1 Exact Match applied successfully.",
            )
        elif occurrences > 1:
            return PatchResult(
                success=False,
                tier_used=1,
                refusal_reason=RefusalReason.REFUSED_AMBIGUOUS_MULTIPLE_EXACT_MATCHES,
                details=f"Search block matches {occurrences} exact locations in file. Refused to guess.",
            )
        return None

    def _try_tier2_normalized(self, source: str, search: str, replace: str) -> Optional[PatchResult]:
        """Tier 2: Normalized whitespace, stripped line endings, and trimmed blank lines."""
        search_lines = [line.rstrip() for line in search.strip("\n").split("\n")]
        source_lines = [line.rstrip() for line in source.split("\n")]

        if not search_lines or len(search_lines) > len(source_lines):
            return None

        # Build collapsed representation (ignoring leading indentation)
        search_collapsed = [line.strip() for line in search_lines if line.strip()]
        if not search_collapsed:
            return None

        window_size = len(search_lines)
        matched_indices: List[int] = []

        for i in range(len(source_lines) - window_size + 1):
            window = [line.strip() for line in source_lines[i : i + window_size] if line.strip()]
            if window == search_collapsed:
                matched_indices.append(i)

        if len(matched_indices) == 1:
            idx = matched_indices[0]
            src_first_line = source.split("\n")[idx]
            search_first_line = search_lines[0] if search_lines else ""

            src_indent = len(src_first_line) - len(src_first_line.lstrip())
            search_indent = len(search_first_line) - len(search_first_line.lstrip())
            delta = src_indent - search_indent

            replace_lines = replace.strip("\n").split("\n")
            adjusted_replace_lines = []
            for r_line in replace_lines:
                if not r_line.strip():
                    adjusted_replace_lines.append("")
                elif delta > 0:
                    adjusted_replace_lines.append(" " * delta + r_line)
                elif delta < 0:
                    trim = min(abs(delta), len(r_line) - len(r_line.lstrip()))
                    adjusted_replace_lines.append(r_line[trim:])
                else:
                    adjusted_replace_lines.append(r_line)

            src_orig_lines = source.split("\n")
            patched_lines = (
                src_orig_lines[:idx]
                + adjusted_replace_lines
                + src_orig_lines[idx + window_size :]
            )
            matched_block = "\n".join(src_orig_lines[idx : idx + window_size])
            return PatchResult(
                success=True,
                tier_used=2,
                patched_code="\n".join(patched_lines),
                matched_search_block=matched_block,
                strategy="whitespace_normalized",
                confidence=1.0,
                details="Tier 2 Normalized Whitespace Match applied.",
            )
        elif len(matched_indices) > 1:
            return PatchResult(
                success=False,
                tier_used=2,
                refusal_reason=RefusalReason.REFUSED_AMBIGUOUS_MULTIPLE_EXACT_MATCHES,
                details=f"Search block matches {len(matched_indices)} locations under whitespace normalization.",
            )

        return None

    def _try_tier3_fuzzy(self, source: str, search: str, replace: str) -> Optional[PatchResult]:
        """Tier 3: Sliding window SequenceMatcher with strict ambiguity margins."""
        search_lines = search.strip("\n").split("\n")
        source_lines = source.split("\n")
        k = len(search_lines)

        if k == 0 or len(source_lines) < k:
            return None

        search_joined = "\n".join(search_lines)
        scores: List[Tuple[float, int]] = []

        # Slide window of size k (and +/- 1 line to allow small drift)
        for delta in [0, 1, -1]:
            cur_k = k + delta
            if cur_k <= 0 or cur_k > len(source_lines):
                continue
            for i in range(len(source_lines) - cur_k + 1):
                window_joined = "\n".join(source_lines[i : i + cur_k])
                ratio = difflib.SequenceMatcher(None, window_joined, search_joined).ratio()
                if ratio >= self.similarity_threshold:
                    scores.append((ratio, i, cur_k))

        if not scores:
            return None

        # Sort descending by similarity ratio
        scores.sort(key=lambda x: x[0], reverse=True)
        best_score, best_idx, best_len = scores[0]

        # Check for ambiguity: find highest-scoring distinct non-overlapping candidate
        runner_up_score = None
        for score, idx, cur_len in scores[1:]:
            # Non-overlapping if it starts after best ends or ends before best starts
            if (idx >= best_idx + best_len) or (idx + cur_len <= best_idx):
                runner_up_score = score
                break

        if runner_up_score is not None:
            if (best_score - runner_up_score) < self.ambiguity_margin:
                return PatchResult(
                    success=False,
                    tier_used=3,
                    refusal_reason=RefusalReason.REFUSED_AMBIGUOUS_FUZZY_MATCH,
                    details=(
                        f"Fuzzy matches are ambiguous: top score {best_score:.2f} vs "
                        f"runner-up {runner_up_score:.2f} (margin < {self.ambiguity_margin})."
                    ),
                )

        # If both boundary lines match exactly but the interior is unrelated,
        # this is the classic wrong-region shape: the search was anchored to a
        # real head/tail while its body came from another revision. A global
        # SequenceMatcher ratio can still clear the threshold because the
        # anchors dominate. Require a high-confidence interior match here too.
        matched_window = "\n".join(source_lines[best_idx : best_idx + best_len])
        search_lines_clean = [line for line in search_lines if line.strip()]
        window_lines_clean = [line for line in matched_window.split("\n") if line.strip()]
        if len(search_lines_clean) >= 3 and len(window_lines_clean) >= 3:
            head_line = search_lines_clean[0].strip()
            tail_line = search_lines_clean[-1].strip()
            unique_heads = [i for i, line in enumerate(source_lines) if line.strip() == head_line]
            unique_tails = [i for i, line in enumerate(source_lines) if line.strip() == tail_line]
            if len(unique_heads) == 1 and len(unique_tails) == 1:
                # When both anchors are unique, a fuzzy window that does not
                # start/end on them is not the region the patch described.
                if (
                    source_lines[best_idx].strip() != head_line
                    or source_lines[best_idx + best_len - 1].strip() != tail_line
                ):
                    return PatchResult(
                        success=False,
                        tier_used=3,
                        refusal_reason=RefusalReason.REFUSED_ANCHOR_INTERIOR_MISMATCH,
                        strategy="sequence_matcher",
                        confidence=best_score,
                        details=(
                            "Unique boundary anchors exist, but the best fuzzy window "
                            "does not align with both of them; refusing to guess the region."
                        ),
                    )
            if (
                search_lines_clean[0].strip() == window_lines_clean[0].strip()
                and search_lines_clean[-1].strip() == window_lines_clean[-1].strip()
            ):
                interior_search = "\n".join(search_lines_clean[1:-1])
                interior_window = "\n".join(window_lines_clean[1:-1])
                interior_ratio = difflib.SequenceMatcher(
                    None, interior_window, interior_search
                ).ratio()
                if interior_ratio < self.tier3_anchor_interior_min_similarity:
                    return PatchResult(
                        success=False,
                        tier_used=3,
                        refusal_reason=RefusalReason.REFUSED_ANCHOR_INTERIOR_MISMATCH,
                        strategy="sequence_matcher",
                        confidence=best_score,
                        details=(
                            "Boundary anchors matched but the interior similarity "
                            f"({interior_ratio:.2%}) is below the "
                            f"{self.tier3_anchor_interior_min_similarity:.0%} safety "
                            "threshold; refusing to replace a potentially unrelated region."
                        ),
                    )

        # Replace the best matched window
        patched_lines = source_lines[:best_idx] + replace.split("\n") + source_lines[best_idx + best_len :]
        matched_block = "\n".join(source_lines[best_idx : best_idx + best_len])
        return PatchResult(
            success=True,
            tier_used=3,
            patched_code="\n".join(patched_lines),
            matched_search_block=matched_block,
            strategy="sequence_matcher",
            confidence=best_score,
            details=f"Tier 3 Fuzzy Match applied with similarity {best_score:.2%}.",
        )

    def _try_tier4_anchors(self, source: str, search: str, replace: str) -> Optional[PatchResult]:
        """Tier 4: Match unique boundary anchor lines (first line and last line) with interior verification."""
        search_lines = [l.strip() for l in search.strip("\n").split("\n") if l.strip()]
        if len(search_lines) < 3:
            return None  # Needs at least head, body, tail to be anchor-safe

        head_anchor = search_lines[0]
        tail_anchor = search_lines[-1]

        source_lines = source.split("\n")
        head_matches = [i for i, l in enumerate(source_lines) if l.strip() == head_anchor]
        tail_matches = [i for i, l in enumerate(source_lines) if l.strip() == tail_anchor]

        if len(head_matches) == 1 and len(tail_matches) == 1:
            head_idx = head_matches[0]
            tail_idx = tail_matches[0]

            if tail_idx > head_idx:
                span_len = tail_idx - head_idx + 1
                expected_len = len(search_lines)
                # Ensure the span length is reasonably bounded (between 0.75x and 1.5x)
                if 0.75 * expected_len <= span_len <= 1.5 * expected_len:
                    matched_block = "\n".join(source_lines[head_idx : tail_idx + 1])
                    span_similarity = difflib.SequenceMatcher(None, matched_block, search).ratio()
                    # Tier 4 replaces the whole span, so require a high-confidence
                    # interior match. A unique head/tail pair alone is not enough.
                    if span_similarity >= self.tier4_min_similarity:
                        patched_lines = (
                            source_lines[:head_idx]
                            + replace.split("\n")
                            + source_lines[tail_idx + 1 :]
                        )
                        return PatchResult(
                            success=True,
                            tier_used=4,
                            patched_code="\n".join(patched_lines),
                            matched_search_block=matched_block,
                            strategy="boundary_anchor",
                            confidence=span_similarity,
                            details=f"Tier 4 Anchor Match applied between lines {head_idx+1} and {tail_idx+1} (span similarity {span_similarity:.2%}).",
                        )

        elif len(head_matches) > 1 or len(tail_matches) > 1:
            return PatchResult(
                success=False,
                tier_used=4,
                refusal_reason=RefusalReason.REFUSED_AMBIGUOUS_ANCHOR_MATCH,
                details=f"Boundary anchors matched multiple regions (head: {len(head_matches)}, tail: {len(tail_matches)}).",
            )

        return None
