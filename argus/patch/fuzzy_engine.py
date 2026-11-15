"""Tiered resilient patch engine with a syntactic gate and strict ambiguity refusal."""

from __future__ import annotations

import bisect
import difflib
import io
import re
import tokenize
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Sequence, Tuple

from argus.patch.ast_checker import ASTChecker, ASTValidationResult, is_python_filename
from argus.patch.parser import PatchBlock
from argus.patch.textio import normalize_newlines, restore_newlines


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
    REFUSED_ALREADY_APPLIED = "REFUSED_ALREADY_APPLIED"


@dataclass
class PatchResult:
    """Outcome of attempting to apply a patch to source code."""
    success: bool
    tier_used: Optional[int] = None
    refusal_reason: RefusalReason = RefusalReason.NONE
    patched_code: Optional[str] = None
    matched_search_block: Optional[str] = None
    # Result of the SYNTACTIC gate (ast.parse + compile). Never a claim of
    # semantic correctness: only the test run can establish that.
    ast_result: Optional[ASTValidationResult] = None
    details: str = ""
    # Provenance for observability. matched_file is filled by the controller,
    # which owns the single-target workspace context.
    matched_file: Optional[str] = None
    strategy: str = ""
    confidence: Optional[float] = None


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
      (?P<comment>\#[^\n]*)
    | (?P<string>(?:[rRbBuUfF]{1,2})?(?:
          \"\"\"(?:\\.|[^\\])*?\"\"\"
        | \'\'\'(?:\\.|[^\\])*?\'\'\'
        | \"(?:\\.|[^\"\\])*\"
        | \'(?:\\.|[^\'\\])*\'
        | (?:\"\"\"|\'\'\'|\"|\').*
      ))
    | (?P<number>\d[\w.]*)
    | (?P<name>[^\W\d]\w*)
    | (?P<op>\*\*=?|//=?|<<=?|>>=?|[<>=!]=|->|:=|\.\.\.|[+\-*/%&|^@]=|\S)
    """,
    re.VERBOSE,
)


def _line_tokens(line: str) -> Tuple[str, ...]:
    """Tokens of one line, ignoring whitespace and comments.

    Operator, identifier, number and string tokens are kept verbatim, so two
    lines with equal token tuples differ only in whitespace/comments.
    """
    return tuple(m.group(0) for m in _TOKEN_RE.finditer(line) if m.lastgroup != "comment")


def _count_tokens(text: str) -> int:
    return sum(len(_line_tokens(line)) for line in text.split("\n"))


def _protected_spans(source: str) -> List[Tuple[int, int]]:
    """Character spans of comments and string literals in Python ``source``.

    Best effort: if tokenisation fails part-way the spans found so far are used.
    """
    starts = [0]
    for m in re.finditer("\n", source):
        starts.append(m.end())

    def offset(pos: Tuple[int, int]) -> int:
        row, col = pos
        return starts[min(row, len(starts)) - 1] + col

    fstring_start = getattr(tokenize, "FSTRING_START", None)
    fstring_end = getattr(tokenize, "FSTRING_END", None)
    spans: List[Tuple[int, int]] = []
    stack: List[int] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if fstring_start is not None and tok.type == fstring_start:
                stack.append(offset(tok.start))
            elif fstring_end is not None and tok.type == fstring_end:
                if stack:
                    begin = stack.pop()
                    if not stack:
                        spans.append((begin, offset(tok.end)))
            elif not stack and tok.type in (tokenize.COMMENT, tokenize.STRING):
                spans.append((offset(tok.start), offset(tok.end)))
    except (tokenize.TokenError, SyntaxError, IndexError):
        pass
    return spans


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _exact_candidates(source: str, search: str, replace: str = "") -> List[int]:
    """Start offsets where ``search`` occurs as a *meaningful* exact match.

    A bare substring hit is not enough: it must not sit inside a longer
    identifier, nor inside a comment/string it does not fully cover. Whole-line
    matches are preferred over sub-line matches.
    """
    positions: List[int] = []
    at = source.find(search)
    while at != -1:
        positions.append(at)
        at = source.find(search, at + 1)
    if not positions:
        return []

    whole: List[int] = []
    partial: List[int] = []
    spans: Optional[List[Tuple[int, int]]] = None
    sub_line_allowed = _count_tokens(search) >= 2
    for pos in positions:
        end = pos + len(search)
        line_start = source.rfind("\n", 0, pos) + 1
        line_end = source.find("\n", end)
        if line_end == -1:
            line_end = len(source)
        after_indent = pos > line_start and not source[line_start:pos].strip()
        if after_indent and replace[:1] in (" ", "\t"):
            # The search was given without the line's indentation but the
            # replacement carries its own: a raw splice would double it. Tier 2
            # handles this case and re-indents the replacement.
            continue
        start_ok = pos == line_start or after_indent
        end_ok = search.endswith("\n") or not source[end:line_end].strip()
        if start_ok and end_ok:
            whole.append(pos)
            continue
        if not sub_line_allowed:
            continue
        if _is_word_char(search[0]) and pos > 0 and _is_word_char(source[pos - 1]):
            continue
        if _is_word_char(search[-1]) and end < len(source) and _is_word_char(source[end]):
            continue
        if spans is None:
            spans = _protected_spans(source)
        if any(b <= pos and end <= e and not (pos == b and end == e) for b, e in spans):
            continue
        partial.append(pos)
    return whole if whole else partial


_INDENT_RE = re.compile(r"[ \t]*")


def _indent_of(line: str) -> str:
    return _INDENT_RE.match(line).group(0)  # type: ignore[union-attr]


def _continuation_flags(lines: Sequence[str]) -> List[bool]:
    """For each line: does it start inside an open bracket (and outside a string)?

    The indentation of such a continuation line is visual alignment, not Python
    structure, so it is the only indentation a tolerant match may disregard. A
    block is assumed to start at bracket depth 0, so a fragment that begins
    mid-expression is simply compared strictly.
    """
    text = "\n".join(lines)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)
    flags = [0] * len(lines)
    in_string = [False] * len(lines)
    depth = 0
    nxt = 0
    for m in _TOKEN_RE.finditer(text):
        row = bisect.bisect_right(starts, m.start()) - 1
        while nxt <= row and nxt < len(lines):
            flags[nxt] = depth
            nxt += 1
        if m.lastgroup == "string" and "\n" in m.group(0):
            end_row = bisect.bisect_right(starts, m.end() - 1) - 1
            for r in range(row + 1, min(end_row, len(lines) - 1) + 1):
                in_string[r] = True
        elif m.lastgroup == "op":
            if m.group(0) in ("(", "[", "{"):
                depth += 1
            elif m.group(0) in (")", "]", "}") and depth:
                depth -= 1
    while nxt < len(lines):
        flags[nxt] = depth
        nxt += 1
    return [flags[i] > 0 and not in_string[i] for i in range(len(lines))]


def _uniform_shift(
    src_indents: Sequence[str],
    search_indents: Sequence[str],
    continuation: Optional[Sequence[bool]] = None,
) -> Optional[Tuple[str, str]]:
    """The single indentation shift turning the search indents into the source indents.

    Returns ``("add", prefix)`` when every source indent is ``prefix + search
    indent``, ``("remove", prefix)`` when every search indent is ``prefix +
    source indent`` and ``("same", "")`` when they are equal. Returns None when
    the relative indentation differs (different nesting, tabs vs spaces), which
    is a structural difference in Python and is never tolerated. Lines flagged
    in ``continuation`` (inside an open bracket) are exempt from the comparison.
    """
    if not src_indents or len(src_indents) != len(search_indents):
        return None
    s0, q0 = src_indents[0], search_indents[0]
    if s0 == q0:
        shift: Tuple[str, str] = ("same", "")
    elif s0.startswith(q0):
        shift = ("add", s0[len(q0) :])
    elif q0.startswith(s0):
        shift = ("remove", q0[len(s0) :])
    else:
        return None
    for i, (s, q) in enumerate(zip(src_indents, search_indents)):
        if continuation is not None and i > 0 and continuation[i]:
            continue
        if shift[0] == "same" and s != q:
            return None
        if shift[0] == "add" and s != shift[1] + q:
            return None
        if shift[0] == "remove" and q != shift[1] + s:
            return None
    return shift


def _reindent(lines: Sequence[str], shift: Tuple[str, str]) -> List[str]:
    mode, prefix = shift
    out: List[str] = []
    for line in lines:
        if not line.strip():
            out.append("")
        elif mode == "add":
            out.append(prefix + line)
        elif mode == "remove":
            out.append(line[len(prefix) :] if line.startswith(prefix) else line.lstrip(" \t"))
        else:
            out.append(line)
    return out


def _window_shift(source_lines: Sequence[str], start: int, block: str) -> Optional[Tuple[str, str]]:
    """Uniform indentation shift between a stripped-matching window and ``block``."""
    block_lines = [line.rstrip() for line in block.strip("\n").split("\n")]
    size = len(block_lines)
    src = [line.rstrip() for line in source_lines[start : start + size] if line.strip()]
    want = [line for line in block_lines if line.strip()]
    return _uniform_shift(
        [_indent_of(x) for x in src], [_indent_of(x) for x in want], _continuation_flags(want)
    )


def _stripped_window_matches(source_lines: Sequence[str], block: str) -> List[int]:
    """Start indices where the stripped, blank-collapsed lines of ``block`` match."""
    block_lines = [line.rstrip() for line in block.strip("\n").split("\n")]
    collapsed = [line.strip() for line in block_lines if line.strip()]
    if not collapsed or len(block_lines) > len(source_lines):
        return []
    size = len(block_lines)
    stripped = [line.rstrip() for line in source_lines]
    hits: List[int] = []
    for i in range(len(stripped) - size + 1):
        window = [line.strip() for line in stripped[i : i + size] if line.strip()]
        if window == collapsed:
            hits.append(i)
    return hits


def _lines_within(part: str, whole: str) -> bool:
    """Do the stripped, non-blank lines of ``part`` occur as a run of lines of ``whole``?"""
    a = [ln.strip() for ln in part.splitlines() if ln.strip()]
    b = [ln.strip() for ln in whole.splitlines() if ln.strip()]
    return bool(a) and any(b[i : i + len(a)] == a for i in range(len(b) - len(a) + 1))


def _replacement_lines(replace: str) -> List[str]:
    """Replacement as lines. Leading/trailing newlines are not content."""
    text = replace.strip("\n")
    return text.split("\n") if text else []


class ResilientPatchEngine:
    """Tiered patch reconciliation engine with a syntactic (not semantic) gate.

    Tiers:
      1. exact match, aligned to a line or token boundary (never inside a
         comment, a string or a longer identifier)
      2. whitespace/indentation-normalised whole-line match
      3. token-equal match: the region must have exactly the same identifier,
         operator, number and string tokens as the search block and may differ
         only in whitespace and comments. A near-miss that changes an operator,
         name or literal (``<`` vs ``<=``, ``return a`` vs ``return b``, ``3``
         vs ``5``) is refused, never applied.
      4. boundary-anchor recovery (experimental, opt-in via ``enable_tier4``)
      5. refusal

    Every applied result is checked with the syntactic gate only (``ast.parse``
    plus ``compile``). Tests, not this engine, decide whether a patch is
    correct. The original line endings are preserved on the patched text.
    """

    def __init__(
        self,
        similarity_threshold: float = 0.82,
        ambiguity_margin: float = 0.10,
        enforce_ast: bool = True,
        enable_tier4: bool = False,
        enable_tier3: bool = True,
    ) -> None:
        # Tier 3 additionally requires this character-level similarity between
        # the search block and the matched region, on top of token equality.
        self.similarity_threshold = similarity_threshold
        # Still carried by ArgusConfig/gateway configuration. Tier 3 candidates
        # are token-equal, so a second distinct candidate is ambiguous
        # regardless of any score margin.
        self.ambiguity_margin = ambiguity_margin
        self.enforce_ast = enforce_ast
        self.enable_tier3 = enable_tier3
        # Tier 4 has narrow demonstrated positive coverage but no successful
        # cases in the adversarial corpus. It is experimental and must be
        # explicitly enabled; the default recovery path stops at Tier 3.
        self.enable_tier4 = enable_tier4
        # Tier 4 replaces an entire anchored span, so it requires a much higher
        # interior similarity than the sliding-window tier. This is deliberately
        # conservative: a false positive here can rewrite unrelated code.
        self.tier4_min_similarity = 0.85

    def apply_patch(
        self,
        source_code: str,
        patch: PatchBlock,
        filename: str = "<target>",
    ) -> PatchResult:
        """Apply a single PatchBlock and run the syntactic gate on the result."""
        return self._apply_one(source_code, patch, filename, validate=True)

    def apply_blocks(
        self,
        source_code: str,
        blocks: Sequence[PatchBlock],
        filename: str = "<target>",
    ) -> PatchResult:
        """Apply blocks in order; the syntactic gate runs once on the final text.

        Unlike repeated ``apply_patch`` calls, an intermediate state that is not
        valid Python (e.g. a ``try:`` block whose ``except`` arrives in the next
        block) is allowed as long as the final state is.
        """
        if not blocks:
            return PatchResult(
                success=False,
                refusal_reason=RefusalReason.REFUSED_EMPTY_PATCH,
                strategy="none",
                details="No patch blocks supplied.",
            )
        working = source_code
        last: Optional[PatchResult] = None
        tier = 0
        applied_pairs = set()
        for block in blocks:
            pair = (normalize_newlines(block.search_text), normalize_newlines(block.replace_text))
            res = self._apply_one(working, block, filename, validate=False)
            if pair in applied_pairs:
                # The response repeats a (search, replace) pair that was already
                # applied. That is one edit, not two: it is skipped when the
                # repeat finds nothing to do (its search text was already
                # rewritten) or would only re-edit its own earlier output (the
                # replacement still contains the search text). A repeat that
                # lands on a different, still-unedited site is a real second
                # edit and is applied.
                if not res.success and res.refusal_reason in (
                    RefusalReason.REFUSED_ALREADY_APPLIED,
                    RefusalReason.REFUSED_NO_MATCH,
                ):
                    continue
                if res.success and _lines_within(res.matched_search_block or "", pair[1]):
                    continue
            if not res.success:
                return res
            applied_pairs.add(pair)
            tier = max(tier, res.tier_used or 0)
            working = res.patched_code if res.patched_code is not None else working
            last = res
        assert last is not None
        last.tier_used = tier or last.tier_used
        return self._verify_and_finalize(last, filename)

    def _apply_one(self, source_code: str, patch: PatchBlock, filename: str, validate: bool) -> PatchResult:
        search = patch.search_text
        replace = patch.replace_text

        if not search.strip():
            return PatchResult(
                success=False,
                refusal_reason=RefusalReason.REFUSED_EMPTY_PATCH,
                strategy="none",
                details="Search block is empty.",
            )

        # Reconcile on \n text; the original line endings are re-emitted below.
        result = self._run_tiers(
            normalize_newlines(source_code),
            normalize_newlines(search),
            normalize_newlines(replace),
        )
        if result.success and validate:
            result = self._verify_and_finalize(result, filename)
        if result.success and result.patched_code is not None:
            result.patched_code = restore_newlines(source_code, result.patched_code)
        return result

    def _run_tiers(self, src: str, search: str, replace: str) -> PatchResult:
        # Tier 1: Exact Match
        t1_res = self._try_tier1_exact(src, search, replace)
        if t1_res:
            return t1_res

        # Tier 2: Normalized Whitespace / Indentation
        t2_res = self._try_tier2_normalized(src, search, replace)
        if t2_res:
            return t2_res

        # The search block is gone but the replacement is already present:
        # report it instead of fuzzily re-applying (duplicating) the edit.
        if replace.strip() and replace.strip() != search.strip():
            if _stripped_window_matches(src.split("\n"), replace):
                return PatchResult(
                    success=False,
                    tier_used=2,
                    refusal_reason=RefusalReason.REFUSED_ALREADY_APPLIED,
                    strategy="already_applied",
                    details=(
                        "Search block was not found, but the replacement text is already "
                        "present in the file; the edit appears to be applied already."
                    ),
                )

        # Tier 3: token-equal tolerant match
        if self.enable_tier3:
            t3_res = self._try_tier3_fuzzy(src, search, replace)
            if t3_res:
                return t3_res

        # Tier 4: Block Anchor Recovery (experimental, opt-in only).
        if self.enable_tier4:
            t4_res = self._try_tier4_anchors(src, search, replace)
            if t4_res:
                return t4_res

        # Tier 5: Safe Refusal
        checked = "Tiers 1-2" + (", 3" if self.enable_tier3 else "") + (", 4" if self.enable_tier4 else "")
        return PatchResult(
            success=False,
            tier_used=5,
            refusal_reason=RefusalReason.REFUSED_NO_MATCH,
            strategy="none",
            details=f"No matching region found for search block (checked {checked}).",
        )

    def _verify_and_finalize(self, candidate_result: PatchResult, filename: str) -> PatchResult:
        """Syntactic gate on the whole synthesized file before allowing success."""
        if not candidate_result.success:
            return candidate_result

        assert candidate_result.patched_code is not None

        is_python = is_python_filename(filename) or filename in ("<target>", "<synthesized>")
        if not is_python and self.enforce_ast:
            return PatchResult(
                success=False,
                tier_used=candidate_result.tier_used,
                refusal_reason=RefusalReason.REFUSED_UNSUPPORTED_LANGUAGE,
                strategy=candidate_result.strategy,
                confidence=candidate_result.confidence,
                details=(
                    f"Refusing to apply to non-Python target '{filename}': no reliable "
                    "language-specific validator is available, so syntax gating cannot be claimed."
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
                    details=f"AST syntax error in synthesized code (syntactic check only): {ast_res.formatted_error()}",
                )

        return candidate_result

    # -------------------------------------------------------------------------
    # Tier Implementations
    # -------------------------------------------------------------------------

    def _try_tier1_exact(self, source: str, search: str, replace: str) -> Optional[PatchResult]:
        """Tier 1: exact match at a line/token boundary, with ambiguity detection."""
        candidates = _exact_candidates(source, search, replace)
        if len(candidates) == 1:
            at = candidates[0]
            stop = at + len(search)
            if (
                not replace.strip()
                and not search.endswith("\n")
                and (at == 0 or source[at - 1] == "\n")
                and source[stop : stop + 1] == "\n"
            ):
                stop += 1  # deleting whole lines: take their line break too
            patched = source[:at] + replace + source[stop:]
            return PatchResult(
                success=True,
                tier_used=1,
                patched_code=patched,
                matched_search_block=search,
                strategy="exact",
                confidence=1.0,
                details="Tier 1 Exact Match applied successfully.",
            )
        if len(candidates) > 1:
            return PatchResult(
                success=False,
                tier_used=1,
                refusal_reason=RefusalReason.REFUSED_AMBIGUOUS_MULTIPLE_EXACT_MATCHES,
                details=f"Search block matches {len(candidates)} exact locations in file. Refused to guess.",
            )
        return None

    def _try_tier2_normalized(self, source: str, search: str, replace: str) -> Optional[PatchResult]:
        """Tier 2: Normalized whitespace, stripped line endings, and trimmed blank lines."""
        search_lines = [line.rstrip() for line in search.strip("\n").split("\n")]
        source_all = source.split("\n")
        window_size = len(search_lines)
        # Only a uniform leading-indent shift is tolerated: the relative
        # indentation of the block must be identical, or a search with different
        # nesting would be spliced in and move code out of its block.
        shifts = {}
        for i in _stripped_window_matches(source_all, search):
            shift = _window_shift(source_all, i, search)
            if shift is not None:
                shifts[i] = shift
        matched_indices = list(shifts)

        if len(matched_indices) == 1:
            idx = matched_indices[0]
            adjusted_replace_lines = _reindent(_replacement_lines(replace), shifts[idx])

            patched_lines = source_all[:idx] + adjusted_replace_lines + source_all[idx + window_size :]
            matched_block = "\n".join(source_all[idx : idx + window_size])
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
        """Tier 3: token-equal match (whitespace/comment differences only).

        A region is a candidate only if its identifier/operator/number/string
        tokens are identical to the search block's. This is what keeps a model's
        misremembered ``<=`` / ``return b`` / ``5`` from silently overwriting the
        real code. More than one distinct candidate is refused as ambiguous.
        """
        search_lines = search.strip("\n").split("\n")
        source_lines = source.split("\n")
        k = len(search_lines)
        if k == 0 or len(source_lines) < k:
            return None

        wanted_list: List[str] = []
        for line in search_lines:
            wanted_list.extend(_line_tokens(line))
        if not wanted_list:
            return None
        wanted = tuple(wanted_list)

        per_line = [_line_tokens(line) for line in source_lines]
        search_joined = "\n".join(search_lines)
        found: List[Tuple[float, int, int]] = []
        shifts: dict = {}
        search_nonblank = [ln for ln in search_lines if ln.strip()]
        search_continuation = _continuation_flags(search_nonblank)
        for delta in (0, 1, -1):
            cur_k = k + delta
            if cur_k <= 0 or cur_k > len(source_lines):
                continue
            for i in range(len(source_lines) - cur_k + 1):
                if not source_lines[i].strip() or not source_lines[i + cur_k - 1].strip():
                    continue
                flat: List[str] = []
                for toks in per_line[i : i + cur_k]:
                    flat.extend(toks)
                if tuple(flat) != wanted:
                    continue
                window = "\n".join(source_lines[i : i + cur_k])
                ratio = difflib.SequenceMatcher(None, window, search_joined).ratio()
                if ratio < self.similarity_threshold:
                    continue
                # Indentation is structure in Python: the region must sit at a
                # uniform indentation shift from the search block. Windows whose
                # line count differs (re-wrapped lines) can only be compared at
                # their first line (continuation lines indent freely).
                region = source_lines[i : i + cur_k]
                region_nonblank = [ln for ln in region if ln.strip()]
                if delta == 0 and len(region_nonblank) == len(search_nonblank):
                    src_pair, want_pair = region_nonblank, search_nonblank
                    continuation: Optional[List[bool]] = search_continuation
                else:
                    src_pair = [region[0]]
                    want_pair = [search_nonblank[0]]
                    continuation = None
                shift = _uniform_shift(
                    [_indent_of(x) for x in src_pair], [_indent_of(x) for x in want_pair], continuation
                )
                if shift is None:
                    continue
                shifts[(i, cur_k)] = shift
                found.append((ratio, i, cur_k))

        if not found:
            return None

        found.sort(key=lambda x: (-x[0], x[2], x[1]))
        best_score, best_idx, best_len = found[0]
        for _score, idx, cur_len in found[1:]:
            if idx >= best_idx + best_len or idx + cur_len <= best_idx:
                return PatchResult(
                    success=False,
                    tier_used=3,
                    refusal_reason=RefusalReason.REFUSED_AMBIGUOUS_FUZZY_MATCH,
                    strategy="token_equal",
                    confidence=best_score,
                    details=(
                        "Several distinct regions have tokens identical to the search block "
                        "(differing only in whitespace/comments); refusing to guess."
                    ),
                )

        new_lines = _reindent(_replacement_lines(replace), shifts[(best_idx, best_len)])
        patched_lines = source_lines[:best_idx] + new_lines + source_lines[best_idx + best_len :]
        matched_block = "\n".join(source_lines[best_idx : best_idx + best_len])
        return PatchResult(
            success=True,
            tier_used=3,
            patched_code="\n".join(patched_lines),
            matched_search_block=matched_block,
            strategy="token_equal",
            confidence=best_score,
            details=(
                "Tier 3 token-equal match applied (region differs from the search block "
                f"only in whitespace/comments; character similarity {best_score:.2%})."
            ),
        )

    def _try_tier4_anchors(self, source: str, search: str, replace: str) -> Optional[PatchResult]:
        """Tier 4: Match unique boundary anchor lines (first line and last line) with interior verification."""
        search_lines = [ln.strip() for ln in search.strip("\n").split("\n") if ln.strip()]
        if len(search_lines) < 3:
            return None  # Needs at least head, body, tail to be anchor-safe

        head_anchor = search_lines[0]
        tail_anchor = search_lines[-1]

        source_lines = source.split("\n")
        head_matches = [i for i, ln in enumerate(source_lines) if ln.strip() == head_anchor]
        tail_matches = [i for i, ln in enumerate(source_lines) if ln.strip() == tail_anchor]

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
                            + _replacement_lines(replace)
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
