"""Regression tests for recall fixes found by re-applying recorded model outputs.

Each test is a minimal synthetic reproduction of a case where the remediated
engine refused an edit that is safe to apply (parser regression on docstring
underlines, bracket continuation indentation, repeated identical blocks).
"""

from __future__ import annotations

from argus.patch import PatchBlock, RefusalReason, ResilientPatchEngine
from argus.patch.fuzzy_engine import _continuation_flags
from argus.patch.parser import PatchParser

DOC_SRC = (
    "class M:\n"
    "    def swap(self, i, j):\n"
    '        """Swap rows.\n'
    "\n"
    "        Examples\n"
    "        ========\n"
    "\n"
    "        >>> M().swap(0, 1)\n"
    '        """\n'
    "        return i, j\n"
)


def test_indented_docstring_underline_is_search_text_not_the_divider():
    search = DOC_SRC.split("\n", 1)[1].rstrip("\n")
    replace = search.replace("return i, j", "return j, i")
    response = f"### File: m.py\n<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE\n"
    blocks = PatchParser.parse(response)
    assert len(blocks) == 1
    assert blocks[0].search_text == search
    assert blocks[0].replace_text == replace
    res = ResilientPatchEngine().apply_blocks(DOC_SRC, blocks, filename="m.py")
    assert res.success and res.tier_used == 1
    assert "return j, i" in (res.patched_code or "")


def test_indented_divider_is_still_accepted_when_no_column0_divider_exists():
    response = "<<<<<<< SEARCH\n    a = 1\n    =======\n    a = 2\n>>>>>>> REPLACE\n"
    blocks = PatchParser.parse(response)
    assert [(b.search_text, b.replace_text) for b in blocks] == [("    a = 1", "    a = 2")]


def test_truncated_block_with_indented_underline_is_still_reported():
    outcome = PatchParser.parse_search_replace_detailed("<<<<<<< SEARCH\n    x\n    ========\n    y\n")
    assert outcome.errors and not outcome.blocks


CALL_SRC = (
    "def build(self):\n"
    "    if self.ready:\n"
    "        filters = [self.a_filter,\n"
    "                   self.b_filter,\n"
    "                   self.c_filter]\n"
    "    return filters\n"
)


def test_continuation_flags_ignore_strings_and_closing_brackets():
    lines = ["x = f(a,", "      b)", "y = '''doc (", "   more", "'''", "z = 1"]
    assert _continuation_flags(lines) == [False, True, False, False, False, False]


def test_bracket_continuation_indentation_may_differ_from_source():
    search = (
        "    filters = [self.a_filter,\n"
        "       self.b_filter,\n"
        "       self.c_filter]\n"
    )
    replace = "    filters = [self.a_filter,\n       self.c_filter]\n"
    res = ResilientPatchEngine().apply_patch(CALL_SRC, PatchBlock(search_text=search, replace_text=replace))
    assert res.success and res.tier_used == 2
    assert res.patched_code == (
        "def build(self):\n"
        "    if self.ready:\n"
        "        filters = [self.a_filter,\n"
        "           self.c_filter]\n"
        "    return filters\n"
    )


def test_statement_indentation_inside_brackets_free_but_block_nesting_still_strict():
    # The continuation exemption must not let a mis-nested *statement* through.
    src = "def f(x):\n    if x:\n        g(x,\n          1)\n        h()\n"
    search = "g(x,\n 1)\n    h()\n"
    res = ResilientPatchEngine().apply_patch(src, PatchBlock(search_text=search, replace_text="g(x, 2)\nh()\n"))
    assert not res.success
    assert res.refusal_reason == RefusalReason.REFUSED_NO_MATCH


def test_identical_repeated_blocks_are_applied_once():
    src = "import os\n\n\ndef f():\n    return 1\n"
    block = PatchBlock(search_text="import os\n", replace_text="import os\nimport sys\n")
    res = ResilientPatchEngine().apply_blocks(src, [block, PatchBlock(block.search_text, block.replace_text)])
    assert res.success
    assert res.patched_code == "import os\nimport sys\n\n\ndef f():\n    return 1\n"


def test_repeated_block_after_a_distinct_one_is_not_reapplied():
    src = "a = 1\nb = 2\n"
    first = PatchBlock(search_text="a = 1", replace_text="a = 10")
    second = PatchBlock(search_text="b = 2", replace_text="b = 20")
    res = ResilientPatchEngine().apply_blocks(src, [first, second, PatchBlock("a = 1", "a = 10")])
    assert res.success and res.patched_code == "a = 10\nb = 20\n"


def test_conflicting_blocks_with_the_same_search_still_refuse():
    # Same SEARCH but a different REPLACE is a conflicting rewrite, not a repeat.
    src = "a = 1\nb = 2\n"
    res = ResilientPatchEngine().apply_blocks(
        src, [PatchBlock("a = 1", "a = 10"), PatchBlock("a = 1", "a = 11")]
    )
    assert not res.success


def test_repeated_block_that_lands_on_another_unedited_site_is_applied():
    # The same pair twice, the second time matching the differently indented
    # second copy (Tier 2) once the first copy has been rewritten.
    src = (
        "def a():\n"
        "    if size:\n"
        "        yield ms\n"
        "\n"
        "def b():\n"
        "    while 1:\n"
        "        if size:\n"
        "            yield ms\n"
    )
    pair = ("    if size:\n        yield ms\n", "    if size:\n        yield ms.copy()\n")
    res = ResilientPatchEngine().apply_blocks(src, [PatchBlock(*pair), PatchBlock(*pair)])
    assert res.success
    assert (res.patched_code or "").count("ms.copy()") == 2
