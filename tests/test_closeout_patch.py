"""Close-out regression tests: Tier 2/3 relative indentation, Tier 1/Tier 2 guards."""

from __future__ import annotations

from argus.patch import PatchBlock, RefusalReason, ResilientPatchEngine

LOOP = (
    "def run(items):\n"
    "    for i in items:\n"
    "        step(i)\n"
    "        log(i)\n"
    "    done()\n"
)


def _apply(src, search, replace, **kw):
    return ResilientPatchEngine(**kw).apply_patch(src, PatchBlock(search_text=search, replace_text=replace))


# --- FIN-11: relative indentation ------------------------------------------


def test_tier2_uniform_indent_shift_is_tolerated_and_replacement_reindented():
    res = _apply(LOOP, "step(i)\nlog(i)\n", "step(i)\nrecord(i)\n")
    assert res.success and res.patched_code is not None
    assert res.tier_used == 2
    assert "        step(i)\n        record(i)\n    done()" in res.patched_code


def test_tier2_search_with_broken_nesting_is_refused():
    broken = "        step(i)\n    log(i)\n"
    res = _apply(LOOP, broken, "        step(i)\n    log(i)\n    extra()\n")
    assert not res.success
    assert res.refusal_reason == RefusalReason.REFUSED_NO_MATCH


def test_tier2_direct_rejects_different_nesting():
    engine = ResilientPatchEngine()
    out = engine._try_tier2_normalized(LOOP, "step(i)\n    log(i)\n", "step(i)\n    log2(i)\n")
    assert out is None  # stripped lines equal, relative indentation not


def test_tier2_probe_does_not_move_code_out_of_loop():
    # The FIN-11 probe: the model dedents ``log(i)`` out of the loop body.
    search = "    for i in items:\n        step(i)\n    log(i)\n"
    replace = "    for i in items:\n        step(i)\n    log2(i)\n"
    res = _apply(LOOP, search, replace)
    assert not res.success
    assert res.patched_code is None


def test_tier3_does_not_mask_the_nesting_refusal_with_tokens_only_match():
    # Same tokens, different nesting must not be applied by the token-equal tier either.
    src = "if a:\n    if b:\n        go()\n    stop()\n"
    res = _apply(src, "if b:\n    go()\n    stop()\n", "if b:\n    go()\n    stop2()\n")
    assert not res.success


def test_tier3_applies_with_uniform_shift_and_reindents_replacement():
    src = "class A:\n    def f(self):\n        total = compute(1, 2)  # sum\n        return total\n"
    search = "      total = compute(1, 2)\n      return total\n"
    replace = "      total = compute(3, 4)\n      return total\n"
    res = _apply(src, search, replace)
    assert res.success and res.tier_used == 3
    assert res.patched_code == "class A:\n    def f(self):\n        total = compute(3, 4)\n        return total\n"


def test_tier3_refuses_when_relative_indentation_differs():
    src = "class A:\n    def f(self):\n        if x:  # c\n            go()\n        stop()\n"
    res = _apply(src, "if x:\n    go()\n    stop()\n", "if x:\n    go()\n    stop2()\n")
    assert not res.success


def test_tier2_tabs_vs_spaces_mismatch_is_refused():
    src = "def f():\n\tfor i in x:\n\t\ta(i)\n\t\tb(i)\n"
    # spaces for the same structure: not a uniform shift of the tab indents
    res = _apply(src, "for i in x:\n    a(i)\n    b(i)\n", "for i in x:\n    a(i)\n")
    assert not res.success


def test_tier2_tab_indented_block_with_uniform_tab_shift_is_applied():
    src = "def f():\n\tfor i in x:\n\t\ta(i)\n\t\tb(i)\n"
    res = _apply(src, "for i in x:\n\ta(i)\n\tb(i)\n", "for i in x:\n\ta(i)\n\tc(i)\n")
    assert res.success and res.patched_code == "def f():\n\tfor i in x:\n\t\ta(i)\n\t\tc(i)\n"


def test_tier2_dedent_direction_removes_prefix_from_replacement():
    res = _apply("a()\nb()\n", "    a()\n    b()\n", "    a()\n    c()\n")
    assert res.success and res.patched_code == "a()\nc()\n"


def test_tier2_ambiguity_refusal_is_kept_when_called_directly():
    # Called directly so Tier 3's own ambiguity refusal cannot mask a removed guard.
    src = "def f():\n    x = 1\n\n\ndef g():\n    x = 1\n"
    out = ResilientPatchEngine()._try_tier2_normalized(src, "x = 1\n", "x = 2\n")
    assert out is not None and not out.success
    assert out.refusal_reason == RefusalReason.REFUSED_AMBIGUOUS_MULTIPLE_EXACT_MATCHES


# --- FIN-14: Tier 1 boundary guards -----------------------------------------


def test_tier1_whole_line_match_preferred_over_longer_line_with_same_prefix():
    # With the end-of-line guard removed both lines count as whole -> ambiguous.
    src = "x = f(a) + g(b)\nx = f(a)\n"
    res = _apply(src, "x = f(a)", "x = h(a)")
    assert res.success and res.tier_used == 1
    assert res.patched_code == "x = f(a) + g(b)\nx = h(a)\n"


def test_tier1_does_not_match_inside_a_longer_identifier_on_the_left():
    res = _apply("ab = 1\nprint(ab)\n", "b = 1", "b = 2")
    assert not res.success


def test_tier1_does_not_match_inside_a_longer_identifier_on_the_right():
    res = _apply("total = valuex\n", "total = value", "total = other")
    assert not res.success


def test_different_indent_width_is_not_a_uniform_shift():
    # 2-space search against a 4-space file: relative indentation differs, so refuse
    # rather than splice a differently-indented replacement into the block.
    src = "def f(a, b):\n    # note\n    total = a + b\n    return total\n"
    res = _apply(src, "def f(a, b):\n  # note\n  total = a + b\n", "def f(a, b):\n  # note\n  total = a - b\n")
    assert not res.success
