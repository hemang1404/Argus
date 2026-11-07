"""WP-2 adversarial patch corpus.

The corpus is synthetic and deliberately does not claim a universal false
positive rate. It measures this implementation against defined mutations and
requires zero incorrect applications for the corpus.
"""

from __future__ import annotations

from collections import Counter

from argus.patch.fuzzy_engine import RefusalReason, ResilientPatchEngine
from argus.patch.parser import PatchBlock
from argus.patch.paths import PatchTargetError, validate_patch_blocks


def _exact_source(i: int) -> tuple[str, str, str, str]:
    marker = f"marker_{i}"
    source = (
        f"def worker_{i}(value):\n"
        f"    result = value + {i}\n"
        f"    {marker} = result\n"
        f"    return {marker}\n"
    )
    search = f"    {marker} = result\n    return {marker}"
    replace = f"    {marker} = result + 1\n    return {marker}"
    expected = source.replace(search, replace)
    return source, search, replace, expected


def test_adversarial_corpus_has_zero_incorrect_applications() -> None:
    engine = ResilientPatchEngine(similarity_threshold=0.82, ambiguity_margin=0.10)
    counts = Counter()
    tiers = Counter()
    bad_variants = Counter()

    for i in range(500):
        variant = i % 7
        source, search, replace, expected = _exact_source(i)
        block = PatchBlock(search_text=search, replace_text=replace)
        expected_result = expected

        if variant == 1:
            # Trailing whitespace drift.
            block = PatchBlock(search_text=search.replace(" = ", " =  ") + "   ", replace_text=replace)
        elif variant == 2:
            # Indentation drift: search and replacement are de-indented.
            block = PatchBlock(
                search_text=search.replace("    ", ""),
                replace_text=replace.replace("    ", ""),
            )
        elif variant == 3:
            # Blank-line drift inside the search block.
            block = PatchBlock(
                search_text=search.replace("\n", "\n\n", 1),
                replace_text=replace,
            )
        elif variant == 4:
            # Two identical implementations: the patch must not guess.
            source = (
                f"def alpha_{i}(value):\n"
                f"    result = value\n"
                f"    return result\n\n"
                f"def beta_{i}(value):\n"
                f"    result = value\n"
                f"    return result\n"
            )
            block = PatchBlock(search_text="    result = value\n    return result", replace_text="    return 0")
            expected_result = None
        elif variant == 5:
            # Matching head/tail anchors with an unrelated interior.
            source = (
                f"def handler_{i}():\n"
                f"    delete_everything()\n"
                f"    wipe_storage()\n"
                f"    return True\n"
            )
            block = PatchBlock(
                search_text=(
                    f"def handler_{i}():\n"
                    "    validate_user_token()\n"
                    "    audit_session()\n"
                    "    verify_mfa()\n"
                    "    return True"
                ),
                replace_text=f"def handler_{i}():\n    return False",
            )
            expected_result = None
        elif variant == 6:
            # Multi-file hunk must be refused before any application.
            blocks = [
                PatchBlock(search_text="x = 1", replace_text="x = 2", file_path=f"a/one_{i}.py"),
                PatchBlock(search_text="x = 1", replace_text="x = 2", file_path=f"b/two_{i}.py"),
            ]
            try:
                validate_patch_blocks(blocks, f"one_{i}.py")
            except PatchTargetError:
                counts["safe_refusal"] += 1
            else:
                counts["incorrect"] += 1
            continue

        result = engine.apply_patch(source, block, filename=f"module_{i}.py")
        if not result.success:
            counts["safe_refusal"] += 1
            continue

        tiers[result.tier_used] += 1
        # Ignore blank-line and trailing-whitespace drift, but preserve leading
        # indentation: indentation is semantically significant in Python. A
        # wrong region or wrong indentation must still fail the oracle.
        def _significant_lines(text: str) -> list[str]:
            return [line.rstrip() for line in text.splitlines() if line.strip()]

        if expected_result is not None and _significant_lines(result.patched_code) == _significant_lines(expected_result):
            counts["correct"] += 1
        else:
            counts["incorrect"] += 1
            bad_variants[variant] += 1

    print(f"adversarial corpus: {dict(counts)}; tiers={dict(tiers)}; bad_variants={dict(bad_variants)}")
    assert counts["incorrect"] == 0
    assert counts["correct"] > 0
    assert counts["safe_refusal"] > 0
    assert sum(counts.values()) == 500
