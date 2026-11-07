"""Unit tests for the Argus 5-Tier Resilient Patch Engine and AST validator."""

import pytest

from argus.patch.ast_checker import ASTChecker
from argus.patch.fuzzy_engine import PatchResult, RefusalReason, ResilientPatchEngine
from argus.patch.parser import PatchBlock, PatchParser


class TestPatchParser:
    def test_parse_search_replace(self):
        text = """
Here is the fix:
<<<<<<< SEARCH
def foo():
    return 1
=======
def foo():
    return 2
>>>>>>> REPLACE
        """
        blocks = PatchParser.parse(text)
        assert len(blocks) == 1
        assert "def foo():" in blocks[0].search_text
        assert "return 1" in blocks[0].search_text
        assert "return 2" in blocks[0].replace_text

    def test_parse_unified_diff(self):
        diff = """
--- a/calculator.py
+++ b/calculator.py
@@ -10,3 +10,3 @@
 def add(a, b):
-    return a - b
+    return a + b
"""
        blocks = PatchParser.parse(diff)
        assert len(blocks) == 1
        assert "return a - b" in blocks[0].search_text
        assert "return a + b" in blocks[0].replace_text

    def test_parse_multi_file_unified_diff(self):
        diff = """
diff --git a/pkg/mod_a.py b/pkg/mod_a.py
--- a/pkg/mod_a.py
+++ b/pkg/mod_a.py
@@ -5,3 +5,3 @@
 def alpha():
-    return "old_a"
+    return "new_a"
diff --git a/pkg/mod_b.py b/pkg/mod_b.py
--- a/pkg/mod_b.py
+++ b/pkg/mod_b.py
@@ -12,3 +12,3 @@
 def beta():
-    return "old_b"
+    return "new_b"
"""
        blocks = PatchParser.parse(diff)
        assert len(blocks) == 2
        assert blocks[0].file_path == "pkg/mod_a.py"
        assert "return \"old_a\"" in blocks[0].search_text
        assert "return \"new_a\"" in blocks[0].replace_text
        assert blocks[1].file_path == "pkg/mod_b.py"
        assert "return \"old_b\"" in blocks[1].search_text
        assert "return \"new_b\"" in blocks[1].replace_text


class TestASTChecker:
    def test_valid_syntax(self):
        code = "def valid():\n    return 42\n"
        res = ASTChecker.validate_python_code(code)
        assert res.is_valid is True
        assert res.error_message is None

    def test_invalid_syntax(self):
        code = "def invalid(\n    return 42\n"
        res = ASTChecker.validate_python_code(code)
        assert res.is_valid is False
        assert res.error_message is not None
        assert res.line_number is not None


class TestResilientPatchEngine:
    @pytest.fixture
    def engine(self):
        return ResilientPatchEngine(similarity_threshold=0.80, ambiguity_margin=0.10)

    def test_tier1_exact_match(self, engine):
        source = "def foo():\n    return 1\n"
        patch = PatchBlock(search_text="return 1", replace_text="return 42")
        res = engine.apply_patch(source, patch)
        assert res.success is True
        assert res.tier_used == 1
        assert res.strategy == "exact"
        assert res.confidence == 1.0
        assert "return 42" in res.patched_code

    def test_tier1_ambiguity_refusal(self, engine):
        source = "def a():\n    x = 1\ndef b():\n    x = 1\n"
        patch = PatchBlock(search_text="x = 1", replace_text="x = 2")
        res = engine.apply_patch(source, patch)
        assert res.success is False
        assert res.tier_used == 1
        assert res.refusal_reason == RefusalReason.REFUSED_AMBIGUOUS_MULTIPLE_EXACT_MATCHES

    def test_tier2_whitespace_crlf(self, engine):
        # Source uses \r\n and slightly different indentation
        source = "def compute():\r\n    total = 0\r\n    return total\r\n"
        # Patch uses \n and no indentation in search block
        patch = PatchBlock(
            search_text="total = 0\nreturn total",
            replace_text="total = 100\nreturn total",
        )
        res = engine.apply_patch(source, patch)
        assert res.success is True
        assert res.tier_used in (1, 2)
        assert "total = 100" in res.patched_code

    def test_tier3_fuzzy_sliding_window(self, engine):
        source = (
            "def calculate_total(cart, tax_rate):\n"
            "    subtotal = sum(item.price for item in cart)\n"
            "    tax = subtotal * tax_rate\n"
            "    return subtotal + tax\n"
        )
        # LLM slightly misremembered variable name or line
        patch = PatchBlock(
            search_text=(
                "    subtotal = sum(item.price for item in cart)\n"
                "    tax = subtotal * 0.05\n"
                "    return subtotal + tax"
            ),
            replace_text=(
                "    subtotal = sum(item.price for item in cart)\n"
                "    tax = subtotal * tax_rate\n"
                "    discount = 5.0\n"
                "    return subtotal + tax - discount"
            ),
        )
        res = engine.apply_patch(source, patch)
        assert res.success is True
        assert res.tier_used == 3
        assert "discount = 5.0" in res.patched_code

    def test_tier4_requires_high_confidence_interior(self, engine):
        """Tier 4 refuses a low-similarity interior instead of replacing the span."""
        source = (
            "def complex_routine(x, y):\n"
            "    step1 = x + 1\n"
            "    step2 = y + 2\n"
            "    step3 = step1 * step2\n"
            "    return step3\n"
        )
        patch = PatchBlock(
            search_text=(
                "def complex_routine(x, y):\n"
                "    something_else = 99\n"
                "    return step3"
            ),
            replace_text=(
                "def complex_routine(x, y):\n"
                "    return (x + 1) * (y + 2)"
            ),
        )
        res = engine.apply_patch(source, patch)
        assert res.success is False
        assert res.refusal_reason == RefusalReason.REFUSED_NO_MATCH

    def test_tier5_safe_refusal_on_no_match(self, engine):
        source = "def foo():\n    return 1\n"
        patch = PatchBlock(search_text="completely_nonexistent_function()", replace_text="pass")
        res = engine.apply_patch(source, patch)
        assert res.success is False
        assert res.tier_used == 5
        assert res.refusal_reason == RefusalReason.REFUSED_NO_MATCH

    def test_universal_ast_gate_blocks_syntax_error(self, engine):
        source = "def calculate(a, b):\n    return a + b\n"
        # Patch introduces a syntax error (unclosed bracket)
        patch = PatchBlock(
            search_text="return a + b",
            replace_text="return (a + b",  # Syntax error!
        )
        res = engine.apply_patch(source, patch, filename="math_module.py")
        assert res.success is False
        assert res.refusal_reason == RefusalReason.REFUSED_SYNTAX_ERROR
        assert "SyntaxError" in res.details

    def test_tier3_ambiguity_refuses_when_competing_region_exists(self, engine):
        """Verify that Tier 3 refuses when another distinct non-overlapping block matches closely."""
        source = (
            "def worker_alpha():\n"
            "    val = compute_item_alpha()\n"
            "    return val * 10\n\n"
            "# middle separator\n\n"
            "def worker_beta():\n"
            "    val = compute_item_beta()\n"
            "    return val * 10\n"
        )
        # Search block is generic enough to match both worker_alpha and worker_beta with near-equal ratio
        patch = PatchBlock(
            search_text=(
                "    val = compute_item_gamma()\n"
                "    return val * 10"
            ),
            replace_text="    return 0",
        )
        res = engine.apply_patch(source, patch)
        assert res.success is False
        assert res.refusal_reason == RefusalReason.REFUSED_AMBIGUOUS_FUZZY_MATCH

    def test_tier4_refuses_alien_interior_despite_matching_anchors(self, engine):
        """Verify Tier 4 refuses when boundary anchors match but the interior is totally alien."""
        source = (
            "def handle_request():\n"
            "    drop_tables()\n"
            "    delete_files()\n"
            "    wipe_storage()\n"
            "    format_drive()\n"
            "    return True\n"
        )
        patch = PatchBlock(
            search_text=(
                "def handle_request():\n"
                "    validate_incoming_user_token()\n"
                "    audit_log_session()\n"
                "    check_rbac_permissions()\n"
                "    verify_mfa()\n"
                "    return True"
            ),
            replace_text="def handle_request():\n    return False",
        )
        # The head and tail match, but the interior is completely alien (<0.60 similarity)
        res = engine.apply_patch(source, patch)
        assert res.success is False
        assert res.tier_used == 5  # Refused by Tier 4, fell through to Tier 5 refusal
        assert res.refusal_reason == RefusalReason.REFUSED_NO_MATCH

    def test_non_python_target_is_refused(self, engine):
        """Non-Python targets are refused because no reliable validator exists."""
        source = "function compute() { return 42; }\n"
        patch = PatchBlock(search_text="return 42;", replace_text="return 100;")
        res = engine.apply_patch(source, patch, filename="service.js")
        assert res.success is False
        assert res.refusal_reason == RefusalReason.REFUSED_UNSUPPORTED_LANGUAGE


class TestTier4Boundaries:
    """Positive, threshold and structural coverage for the experimental Tier 4 path."""

    SEARCH = (
        "def f():\n"
        "    value = 8\n"
        "    pass\n"
        "    pass\n"
        "    return value"
    )
    SOURCE = (
        "def f():\n"
        "    value = 8\n"
        "    pass\n"
        "    # note\n"
        "    pass\n"
        "    pass\n"
        "    return value\n"
    )
    REPLACE = "def f():\n    return 0"

    def test_tier4_is_disabled_by_default(self):
        engine = ResilientPatchEngine()
        res = engine.apply_patch(
            self.SOURCE,
            PatchBlock(search_text=self.SEARCH, replace_text=self.REPLACE),
            filename="module.py",
        )
        assert res.success is False
        assert res.tier_used == 5
        assert res.refusal_reason == RefusalReason.REFUSED_NO_MATCH

    def test_positive_anchor_recovery_at_threshold(self):
        engine = ResilientPatchEngine(enable_tier4=True)
        res = engine.apply_patch(
            self.SOURCE,
            PatchBlock(search_text=self.SEARCH, replace_text=self.REPLACE),
            filename="module.py",
        )

        assert res.success is True
        assert res.tier_used == 4
        assert res.confidence is not None and res.confidence >= 0.85
        assert res.patched_code == "def f():\n    return 0\n"
        assert "# note" not in res.patched_code

    def test_just_below_threshold_is_refused(self):
        engine = ResilientPatchEngine()
        below = self.SOURCE.replace("value = 8", "value = 7")
        res = engine.apply_patch(
            below,
            PatchBlock(search_text=self.SEARCH, replace_text=self.REPLACE),
            filename="module.py",
        )
        assert res.success is False
        assert res.tier_used == 5
        assert res.refusal_reason == RefusalReason.REFUSED_NO_MATCH

        # Directly demonstrate the threshold boundary: the same candidate is
        # accepted at the production 0.85 threshold and rejected just above it.
        candidate = engine._try_tier4_anchors(self.SOURCE, self.SEARCH, self.REPLACE)
        assert candidate is not None and candidate.success is True
        engine.tier4_min_similarity = 0.86
        assert engine._try_tier4_anchors(self.SOURCE, self.SEARCH, self.REPLACE) is None

    def test_span_length_boundaries_refuse(self):
        engine = ResilientPatchEngine()
        too_long = (
            "def f():\n"
            "    value = 8\n"
            "    pass\n"
            "    # note\n"
            "    pass\n"
            "    pass\n"
            "    pass\n"
            "    pass\n"
            "    pass\n"
            "    return value\n"
        )
        assert engine._try_tier4_anchors(too_long, self.SEARCH, self.REPLACE) is None
        res = engine.apply_patch(
            too_long,
            PatchBlock(search_text=self.SEARCH, replace_text=self.REPLACE),
            filename="module.py",
        )
        assert res.success is False

    def test_duplicate_boundary_anchors_are_refused(self):
        engine = ResilientPatchEngine()
        source = (
            "def f():\n"
            "    value = 8\n"
            "    return value\n"
            "def f():\n"
            "    value = 9\n"
            "    return value\n"
        )
        res = engine._try_tier4_anchors(source, self.SEARCH, self.REPLACE)
        assert res is not None
        assert res.success is False
        assert res.refusal_reason == RefusalReason.REFUSED_AMBIGUOUS_ANCHOR_MATCH

    def test_indentation_sensitive_replacement_is_not_applied(self):
        engine = ResilientPatchEngine()
        source = (
            "def outer():\n"
            "    def inner():\n"
            "        value = 8\n"
            "        pass\n"
            "        return value\n"
            "    return inner\n"
        )
        search = (
            "def inner():\n"
            "        value = 8\n"
            "        pass\n"
            "        pass\n"
            "        return value"
        )
        # The replacement is de-indented relative to the nested function and
        # would produce invalid/incorrect structure if applied.
        res = engine.apply_patch(
            source,
            PatchBlock(search_text=search, replace_text="def inner():\n    return 0"),
            filename="module.py",
        )
        assert res.success is False
        assert res.refusal_reason == RefusalReason.REFUSED_SYNTAX_ERROR

