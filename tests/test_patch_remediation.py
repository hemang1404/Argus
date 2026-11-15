"""Regression tests for the PATCH audit remediation (PATCH-01..16, ARCH-06).

Everything is deterministic and offline: model output is scripted by patching
``RecoveryController._call_model``; all files live under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from argus.config import ArgusConfig
from argus.patch import ASTChecker, PatchBlock, PatchParser, RefusalReason, ResilientPatchEngine
from argus.patch.textio import (
    UnsupportedEncodingError,
    decode_source,
    encode_source,
    normalize_newlines,
    restore_newlines,
)
from argus.runtime.candidate import AuthoritativePatchApplier, CandidateArtifactStore
from argus.runtime.controller import RecoveryController
from argus.runtime.workspace import (
    TargetNotFoundError,
    TargetOutsideWorkspaceError,
    WorkspaceManager,
)

CALC = "def add(a, b):\n    return a - b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
GOOD = (
    "<<<<<<< SEARCH\ndef add(a, b):\n    return a - b\n=======\n"
    "def add(a, b):\n    return a + b\n>>>>>>> REPLACE"
)
CMD = f'"{sys.executable}" -m pytest -q test_calc.py -p no:cacheprovider'


def P(search: str, replace: str) -> PatchBlock:
    return PatchBlock(search_text=search, replace_text=replace)


@pytest.fixture
def engine() -> ResilientPatchEngine:
    return ResilientPatchEngine()


@pytest.fixture
def isolated_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make argus_ws_* workspaces land in a private temp dir so leaks are observable."""
    tmpdir = tmp_path / "systmp"
    tmpdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmpdir))
    return tmpdir


def make_proj(root: Path, calc: bytes = CALC.encode(), test: str = TEST) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "calc.py").write_bytes(calc)
    (root / "test_calc.py").write_text(test, encoding="utf-8")
    return root


def run_recovery(root: Path, responses: list[str], **kwargs):
    seq = list(responses)
    ctl = RecoveryController(config=ArgusConfig(provider="antigravity", max_attempts=2))

    def fake(*_a, **_k):
        return (seq.pop(0) if len(seq) > 1 else seq[0], 10, 10)

    with patch.object(ctl, "_call_model", side_effect=fake):
        return ctl.run(target_file="calc.py", test_command=CMD, workspace_root=root, **kwargs)


# --------------------------------------------------------------------------- textio


def test_decode_source_bom_cookie_and_failure() -> None:
    d = decode_source(b"\xef\xbb\xbfx = 1\n")
    assert d.text == "x = 1\n" and d.bom == b"\xef\xbb\xbf" and d.encoding == "utf-8"
    assert encode_source(d.text, d.encoding, d.bom) == b"\xef\xbb\xbfx = 1\n"

    latin = b"# -*- coding: latin-1 -*-\n# caf\xe9\n"
    d2 = decode_source(latin)
    assert "café" in d2.text
    assert encode_source(d2.text, d2.encoding, d2.bom) == latin

    with pytest.raises(UnsupportedEncodingError):
        decode_source(b"# caf\xe9 with no declaration\n")  # not UTF-8, no cookie: never U+FFFD
    with pytest.raises(UnsupportedEncodingError):
        encode_source("snowman \u2603", "latin-1")


def test_restore_newlines_uniform_and_mixed() -> None:
    assert restore_newlines("a\r\nb\r\n", "a\nX\nb\n") == "a\r\nX\r\nb\r\n"
    assert restore_newlines("a\rb\r", "a\nb\nc\n") == "a\rb\rc\r"
    assert restore_newlines("a\nb\n", "a\nb\n") == "a\nb\n"
    mixed = "a\r\nb\nc\r\n"
    out = restore_newlines(mixed, normalize_newlines(mixed).replace("b", "B"))
    assert out == "a\r\nB\nc\r\n"  # unchanged lines keep their own ending


# --------------------------------------------------------------------------- PATCH-01 EOL


@pytest.mark.parametrize("eol", ["\r\n", "\n", "\r"])
@pytest.mark.parametrize(
    "search, replace",
    [
        ("    return a - b", "    return a + b"),  # tier 1
        ("return a - b", "return a + b"),  # tier 1, search given without indentation
        ("    return a-b", "    return a + b"),  # tier 3
    ],
)
def test_engine_preserves_line_endings(engine: ResilientPatchEngine, eol: str, search: str, replace: str) -> None:
    src = f"def add(a, b):{eol}    return a - b{eol}"
    res = engine.apply_patch(src, P(search, replace))
    assert res.success, res.details
    assert res.patched_code == f"def add(a, b):{eol}    return a + b{eol}"


def test_engine_search_with_crlf_matches_lf_source(engine: ResilientPatchEngine) -> None:
    res = engine.apply_patch(CALC, P("def add(a, b):\r\n    return a - b", "def add(a, b):\r\n    return a + b"))
    assert res.success and res.patched_code == "def add(a, b):\n    return a + b\n"


def test_crlf_end_to_end_tested_bytes_equal_applied_bytes(tmp_path: Path) -> None:
    crlf = CALC.replace("\n", "\r\n").encode()
    root = make_proj(tmp_path / "p", calc=crlf, test=TEST)
    out = run_recovery(root, [GOOD])
    assert out.status == "SUCCESS", out.message
    assert (root / "calc.py").read_bytes() == crlf  # untouched until applied
    expected = b"def add(a, b):\r\n    return a + b\r\n"
    assert out.patched_content.encode() == expected
    res = AuthoritativePatchApplier.apply(out.run_id, workspace_root=root)
    assert res.success, res.message
    assert (root / "calc.py").read_bytes() == expected


def test_workspace_writes_lf_file_without_newline_translation(tmp_path: Path) -> None:
    root = make_proj(tmp_path / "p")
    with WorkspaceManager(repo_root=root, target_file="calc.py") as ws:
        res = ws.apply_patch(P("    return a - b", "    return a + b"), ResilientPatchEngine())
        assert res.success
        assert (ws.temp_dir / "calc.py").read_bytes() == b"def add(a, b):\n    return a + b\n"


# --------------------------------------------------------------------------- PATCH-05/06 encoding


def test_bom_file_is_recoverable_and_bom_preserved(tmp_path: Path) -> None:
    bom_src = b"\xef\xbb\xbf" + CALC.encode()
    root = make_proj(tmp_path / "p", calc=bom_src)
    out = run_recovery(root, [GOOD])
    assert out.status == "SUCCESS", out.message
    assert "\ufeff" not in out.patched_content
    res = AuthoritativePatchApplier.apply(out.run_id, workspace_root=root)
    assert res.success, res.message
    assert (root / "calc.py").read_bytes() == b"\xef\xbb\xbf" + b"def add(a, b):\n    return a + b\n"


def test_cookie_encoded_file_round_trips_bytes(tmp_path: Path) -> None:
    src = b"# -*- coding: latin-1 -*-\n# caf\xe9\ndef add(a, b):\n    return a - b\n"
    root = make_proj(tmp_path / "p", calc=src)
    out = run_recovery(root, [GOOD])
    assert out.status == "SUCCESS", out.message
    assert AuthoritativePatchApplier.apply(out.run_id, workspace_root=root).success
    final = (root / "calc.py").read_bytes()
    assert b"caf\xe9" in final and b"\xef\xbf\xbd" not in final and b"a + b" in final


def test_undecodable_target_fails_cleanly_and_leaks_no_workspace(tmp_path: Path, isolated_tmp: Path) -> None:
    root = make_proj(tmp_path / "p", calc=b"# caf\xe9 no cookie\ndef add(a, b):\n    return a - b\n")
    with pytest.raises(UnsupportedEncodingError):
        WorkspaceManager(repo_root=root, target_file="calc.py").setup()
    assert not list(isolated_tmp.glob("argus_ws_*"))
    assert (root / "calc.py").read_bytes().startswith(b"# caf\xe9")


def test_apply_refuses_when_target_became_undecodable(tmp_path: Path) -> None:
    root = make_proj(tmp_path / "p")
    base = hashlib.sha256((root / "calc.py").read_bytes()).hexdigest()
    new = "def add(a, b):\n    return a + b\n"
    CandidateArtifactStore.save_run_artifact(
        run_id="run_enc", target_file="calc.py", test_command="t", baseline_hash=base, status="SUCCESS",
        verified=True, tier_used=1, attempts=1, cost_usd=0.0, duration_ms=1.0, patched_content=new,
        unified_diff="d", workspace_root=root,
    )
    # target bytes are not decodable but still hash to the baseline we recorded
    bad = b"# caf\xe9\n" + CALC.encode()
    (root / "calc.py").write_bytes(bad)
    meta, _ = CandidateArtifactStore.load_candidate("run_enc", root)
    assert meta.baseline_hash == base
    res = AuthoritativePatchApplier.apply("run_enc", workspace_root=root)
    assert not res.success and res.error_code in ("UNSUPPORTED_ENCODING", "STALE_TARGET_CONFLICT")
    assert (root / "calc.py").read_bytes() == bad


# --------------------------------------------------------------------------- PATCH-02 snapshot


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(cwd), check=True, capture_output=True,
    )


def test_snapshot_keeps_fixture_dirs_and_binary_fixtures(tmp_path: Path) -> None:
    root = make_proj(tmp_path / "p")
    for rel in ("data/x.txt", "tests/data/case.json", "build/b.txt", "dist/d.txt", "results/r.txt",
                "packages/p.txt", "scratch/s.txt", "logs/l.txt", "fixture.zip", "docs/spec.pdf", "a/b/in.docx"):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"\x00bin")
    with WorkspaceManager(repo_root=root, target_file="calc.py") as ws:
        for rel in ("data/x.txt", "tests/data/case.json", "build/b.txt", "dist/d.txt", "results/r.txt",
                    "packages/p.txt", "scratch/s.txt", "logs/l.txt", "fixture.zip", "docs/spec.pdf", "a/b/in.docx"):
            assert (ws.temp_dir / rel).read_bytes() == b"\x00bin", rel


def test_snapshot_excludes_only_internals_and_root_caches(tmp_path: Path) -> None:
    root = make_proj(tmp_path / "p")
    for rel in (".git/HEAD", ".argus/runs/r/m.json", "node_modules/m/i.js", ".venv/lib/x.py",
                "pkg/__pycache__/m.pyc", "pkg/keep.py", "sub/node_modules/x.js"):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    with WorkspaceManager(repo_root=root, target_file="calc.py") as ws:
        t = ws.temp_dir
        assert not (t / ".git").exists() and not (t / ".argus").exists()
        assert not (t / "node_modules").exists() and not (t / ".venv").exists()
        assert not (t / "pkg" / "__pycache__").exists()
        assert (t / "pkg" / "keep.py").exists()
        assert (t / "sub" / "node_modules" / "x.js").exists()  # only ROOT-level caches are skipped


def test_snapshot_exclude_is_configurable(tmp_path: Path) -> None:
    root = make_proj(tmp_path / "p")
    (root / "heavy").mkdir()
    (root / "heavy" / "big.bin").write_bytes(b"1")
    with WorkspaceManager(repo_root=root, target_file="calc.py", snapshot_exclude=["heavy"]) as ws:
        assert not (ws.temp_dir / "heavy").exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_snapshot_respects_gitignore_and_copies_untracked(tmp_path: Path) -> None:
    root = make_proj(tmp_path / "p")
    (root / ".gitignore").write_text("secret.env\nout/\n", encoding="utf-8")
    (root / "secret.env").write_text("TOKEN=x")
    (root / "out").mkdir()
    (root / "out" / "o.txt").write_text("o")
    (root / "data").mkdir()
    (root / "data" / "tracked.txt").write_text("t")
    _git(root, "init")
    _git(root, "add", "calc.py", "test_calc.py", ".gitignore", "data/tracked.txt")
    _git(root, "commit", "-m", "i")
    (root / "untracked.txt").write_text("u")
    (root / "calc.py").write_text(CALC + "# dirty\n")
    with WorkspaceManager(repo_root=root, target_file="calc.py") as ws:
        t = ws.temp_dir
        assert (t / "data" / "tracked.txt").exists() and (t / "untracked.txt").exists()
        assert not (t / "secret.env").exists() and not (t / "out").exists()
        assert "# dirty" in (t / "calc.py").read_text()  # working-tree content, not HEAD
    with WorkspaceManager(repo_root=root, target_file="calc.py", snapshot_respect_gitignore=False) as ws:
        assert (ws.temp_dir / "secret.env").exists()


def test_snapshot_test_needing_fixture_reaches_success(tmp_path: Path) -> None:
    test = (
        "import zipfile\nfrom calc import add\n\n\ndef test_add():\n"
        "    assert zipfile.is_zipfile('data/fixture.zip')\n    assert add(2, 3) == 5\n"
    )
    root = make_proj(tmp_path / "p", test=test)
    (root / "data").mkdir()
    import zipfile

    with zipfile.ZipFile(root / "data" / "fixture.zip", "w") as z:
        z.writestr("a.txt", "a")
    out = run_recovery(root, [GOOD])
    assert out.status == "SUCCESS", out.message


# --------------------------------------------------------------------------- PATCH-10 / 07-09 / 13-15 workspace


def test_target_outside_workspace_is_refused_without_copying(tmp_path: Path, isolated_tmp: Path) -> None:
    root = make_proj(tmp_path / "proj")
    other = tmp_path / "other"
    other.mkdir()
    (other / "t.py").write_text("x = 1\n")
    (other / "unrelated.txt").write_text("private")
    with pytest.raises(TargetOutsideWorkspaceError):
        WorkspaceManager(repo_root=root, target_file=other / "t.py")
    with pytest.raises(TargetOutsideWorkspaceError):
        WorkspaceManager(repo_root=root, target_file="../other/t.py")
    assert not list(isolated_tmp.glob("argus_ws_*"))


def test_worktree_mode_is_gone(tmp_path: Path) -> None:
    root = make_proj(tmp_path / "p")
    with pytest.raises(ValueError, match="worktree"):
        WorkspaceManager(repo_root=root, target_file="calc.py", use_worktree=True)
    import argus.runtime.workspace as w

    assert not hasattr(w, "cleanup_stale_worktrees")
    assert not hasattr(ArgusConfig(), "use_worktree")


def test_missing_target_raises_typed_error_and_leaks_nothing(tmp_path: Path, isolated_tmp: Path) -> None:
    root = make_proj(tmp_path / "p")
    with pytest.raises(TargetNotFoundError):
        WorkspaceManager(repo_root=root, target_file="nope.py").setup()
    assert issubclass(TargetNotFoundError, FileNotFoundError)
    assert not list(isolated_tmp.glob("argus_ws_*"))


# --------------------------------------------------------------------------- PATCH-03 Tier 3


@pytest.mark.parametrize(
    "source, search, replace",
    [
        ("def f(a, b):\n    if a < b:\n        return 1\n    return 0\n", "    if a <= b:\n        return 1", "    if a <= b:\n        return 2"),
        ("def f(a, b):\n    if a < b:\n        return 1\n    return 0\n", "    if a > b:\n        return 1", "    if a > b:\n        return 2"),
        ("def f(a, b):\n    return a\n", "def f(a, b):\n    return b", "def f(a, b):\n    return 0"),
        ("RETRIES = 3\nTIMEOUT = 10\n", "RETRIES = 5", "RETRIES = 9"),
        ("def f(x):\n    return x >= 1 and x <= 9\n", "    return x > 1 and x < 9", "    return True"),
    ],
)
def test_tier3_refuses_near_misses(engine: ResilientPatchEngine, source: str, search: str, replace: str) -> None:
    res = engine.apply_patch(source, P(search, replace))
    assert not res.success
    assert res.refusal_reason == RefusalReason.REFUSED_NO_MATCH


def test_tier3_accepts_whitespace_and_comment_only_drift(engine: ResilientPatchEngine) -> None:
    src = "def f(a,b):\n    if a<b:  # lt\n        return 1\n    return 0\n"
    res = engine.apply_patch(src, P("    if a < b:\n        return 1", "    if a < b:\n        return 2"))
    assert res.success and res.tier_used == 3 and res.strategy == "token_equal"
    assert "return 2" in res.patched_code and "return 0" in res.patched_code


def test_tier3_can_be_switched_off(tmp_path: Path) -> None:
    src = "def f(a,b):\n    return a+b\n"
    assert ResilientPatchEngine().apply_patch(src, P("    return a + b", "    return 0")).success
    off = ResilientPatchEngine(enable_tier3=False).apply_patch(src, P("    return a + b", "    return 0"))
    assert not off.success and off.refusal_reason == RefusalReason.REFUSED_NO_MATCH


def test_tier3_does_not_add_blank_line_for_trailing_newline_replacement(engine: ResilientPatchEngine) -> None:
    src = "def f(a,b):\n    return a+b\n\ndef g():\n    pass\n"
    res = engine.apply_patch(src, P("    return a + b", "    return a + b + 1\n"))
    assert res.success and res.tier_used == 3
    assert res.patched_code == "def f(a,b):\n    return a + b + 1\n\ndef g():\n    pass\n"


def test_tier4_is_opt_in_everywhere() -> None:
    assert ResilientPatchEngine().enable_tier4 is False
    assert ArgusConfig().enable_tier4 is False
    ctl = RecoveryController(config=ArgusConfig(provider="antigravity"))
    assert ctl.patch_engine.enable_tier4 is False
    search = "def f():\n    value = 8\n    pass\n    pass\n    return value"
    source = "def f():\n    value = 8\n    pass\n    # note\n    pass\n    pass\n    return value\n"
    replace = "def f():\n    return 0"
    assert not ResilientPatchEngine().apply_patch(source, P(search, replace)).success
    on = ResilientPatchEngine(enable_tier4=True).apply_patch(source, P(search, replace))
    assert on.success and on.tier_used == 4


# --------------------------------------------------------------------------- PATCH-04 Tier 1 alignment


def test_tier1_does_not_edit_a_comment(engine: ResilientPatchEngine) -> None:
    src = "def f():\n    # fix: return 0 when empty\n    return compute()\n"
    res = engine.apply_patch(src, P("return 0", "return 1"))
    assert not res.success


def test_tier1_does_not_edit_inside_longer_identifier(engine: ResilientPatchEngine) -> None:
    src = "def f(aa, bb):\n    return aa < bb\n"
    res = engine.apply_patch(src, P("a < b", "a <= b"))
    assert not res.success


def test_tier1_prefers_whole_line_over_substring(engine: ResilientPatchEngine) -> None:
    src = "x = 1\ny = x = 1\n"
    res = engine.apply_patch(src, P("x = 1", "x = 2"))
    assert res.success and res.tier_used == 1 and res.patched_code == "x = 2\ny = x = 1\n"


def test_tier1_subline_token_aligned_edit_still_works(engine: ResilientPatchEngine) -> None:
    src = "def f(a, b):\n    return a - b\n"
    res = engine.apply_patch(src, P("a - b", "a + b"))
    assert res.success and res.tier_used == 1 and "return a + b" in res.patched_code


def test_tier1_ambiguity_still_refused(engine: ResilientPatchEngine) -> None:
    src = "x = 1\nx = 1\n"
    res = engine.apply_patch(src, P("x = 1", "x = 2"))
    assert not res.success and res.refusal_reason == RefusalReason.REFUSED_AMBIGUOUS_MULTIPLE_EXACT_MATCHES


def test_tier1_not_confused_by_string_content(engine: ResilientPatchEngine) -> None:
    src = 'MSG = "return 0 means ok"\n\ndef f():\n    return 0\n'
    res = engine.apply_patch(src, P("return 0", "return 1"))
    assert res.success and res.patched_code.endswith("    return 1\n") and "return 0 means ok" in res.patched_code


# --------------------------------------------------------------------------- PATCH-12 gate


def test_gate_catches_compile_time_errors_and_is_labelled_syntactic() -> None:
    assert ASTChecker.validate_python_code("x = 1\n").is_valid
    assert ASTChecker.validate_python_code("x = 1\n").level == "syntactic"
    for bad in ("return 1\n", "yield 1\n", "break\n", "def f():\n    nonlocal q\n"):
        assert not ASTChecker.validate_python_code(bad).is_valid, bad


def test_engine_rejects_module_level_return(engine: ResilientPatchEngine) -> None:
    res = engine.apply_patch("x = 1\n", P("x = 1", "return 1"))
    assert not res.success and res.refusal_reason == RefusalReason.REFUSED_SYNTAX_ERROR


@pytest.mark.parametrize("name", ["m.PY", "m.py", "stub.pyi", "m.pyw"])
def test_gate_python_suffixes_case_insensitive(engine: ResilientPatchEngine, name: str) -> None:
    assert engine.apply_patch("x = 1\n", P("x = 1", "x = 2"), filename=name).success


def test_non_python_target_refused(engine: ResilientPatchEngine) -> None:
    res = engine.apply_patch("a=1\n", P("a=1", "a=2"), filename="m.js")
    assert not res.success and res.refusal_reason == RefusalReason.REFUSED_UNSUPPORTED_LANGUAGE


def test_apply_blocks_validates_final_state_only(engine: ResilientPatchEngine) -> None:
    src = "def f():\n    work()\n"
    blocks = [P("    work()", "    try:\n        work()"), P("        work()", "        work()\n    except OSError:\n        pass")]
    assert not engine.apply_patch(src, blocks[0]).success  # intermediate state is invalid on its own
    res = engine.apply_blocks(src, blocks)
    assert res.success, res.details
    assert res.patched_code == "def f():\n    try:\n        work()\n    except OSError:\n        pass\n"
    bad = engine.apply_blocks(src, [blocks[0]])
    assert not bad.success and bad.refusal_reason == RefusalReason.REFUSED_SYNTAX_ERROR


# --------------------------------------------------------------------------- PATCH-13 already applied


def test_already_applied_edit_is_reported_not_duplicated(engine: ResilientPatchEngine) -> None:
    patch_ = P("def add(a, b):\n    return a - b", "def add(a, b):\n    return a + b")
    first = engine.apply_patch(CALC, patch_)
    assert first.success
    again = engine.apply_patch(first.patched_code, patch_)
    assert not again.success and again.refusal_reason == RefusalReason.REFUSED_ALREADY_APPLIED


# --------------------------------------------------------------------------- PATCH-11/14 parser

DELETION = (
    "<<<<<<< SEARCH\nold_line()\n=======\n>>>>>>> REPLACE\n"
    "<<<<<<< SEARCH\nfoo = 1\n=======\nfoo = 2\n>>>>>>> REPLACE\n"
)


def test_pure_deletion_block_parses_and_does_not_swallow_next() -> None:
    blocks = PatchParser.parse(DELETION)
    assert [(b.search_text, b.replace_text) for b in blocks] == [("old_line()", ""), ("foo = 1", "foo = 2")]
    only = PatchParser.parse("<<<<<<< SEARCH\nold_line()\n=======\n>>>>>>> REPLACE")
    assert len(only) == 1 and only[0].replace_text == ""


def test_deletion_applies_end_to_end(engine: ResilientPatchEngine) -> None:
    block = PatchParser.parse("<<<<<<< SEARCH\nold_line()\n=======\n>>>>>>> REPLACE")[0]
    res = engine.apply_patch("a = 1\nold_line()\nb = 2\n", block)
    assert res.success and res.patched_code == "a = 1\nb = 2\n"


def test_truncated_block_is_reported_and_not_merged() -> None:
    text = (
        "<<<<<<< SEARCH\na = 1\n=======\nb = 2\n"  # no REPLACE terminator
        "<<<<<<< SEARCH\nc = 3\n=======\nd = 4\n>>>>>>> REPLACE\n"
    )
    outcome = PatchParser.parse_search_replace_detailed(text)
    assert outcome.errors and "Truncated" in outcome.errors[0]
    assert [(b.search_text, b.replace_text) for b in outcome.blocks] == [("c = 3", "d = 4")]
    assert PatchParser.parse(text) == []  # damaged response: apply nothing
    eof = PatchParser.parse_search_replace_detailed("<<<<<<< SEARCH\na\n=======\nb\n")
    assert eof.errors and not eof.blocks
    assert PatchParser.parse_search_replace_detailed("<<<<<<< SEARCH\na\n>>>>>>> REPLACE\n").errors


def test_parser_still_handles_path_hints_and_crlf_and_fences() -> None:
    text = "```\r\n<<<<<<< SEARCH calc.py\r\nx = 1\r\n=======\r\nx = 2\r\n>>>>>>> REPLACE\r\n```"
    blocks = PatchParser.parse(text)
    assert len(blocks) == 1 and blocks[0].file_path == "calc.py" and blocks[0].replace_text == "x = 2"
    pre = "### File: pkg/m.py\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n"
    assert PatchParser.parse(pre)[0].file_path == "pkg/m.py"


def test_line_annotations_stripped_only_when_every_line_has_them() -> None:
    annotated = "<<<<<<< SEARCH\n 1 | a = 1\n 2 | b = 2\n=======\n 1 | a = 9\n 2 | b = 2\n>>>>>>> REPLACE"
    b = PatchParser.parse(annotated)[0]
    assert b.search_text == "a = 1\nb = 2" and b.replace_text == "a = 9\nb = 2"
    legit = "<<<<<<< SEARCH\nx = (a\n  1 | b)\n=======\nx = (a\n  1 | c)\n>>>>>>> REPLACE"
    lb = PatchParser.parse(legit)[0]
    assert lb.search_text == "x = (a\n  1 | b)" and lb.replace_text == "x = (a\n  1 | c)"


def test_unified_diff_hunk_not_cut_at_removed_dashes_line() -> None:
    diff = (
        "--- a/m.py\n+++ b/m.py\n@@ -1,3 +1,3 @@\n"
        " keep = 1\n--- x\n+y = 2\n tail = 3\n"
    )
    blocks = PatchParser.parse_unified_diff(diff)
    assert len(blocks) == 1
    assert blocks[0].search_text == "keep = 1\n-- x\ntail = 3"
    assert blocks[0].replace_text == "keep = 1\ny = 2\ntail = 3"
    assert blocks[0].file_path == "m.py"


def test_unified_diff_multiple_files_and_hunks() -> None:
    diff = (
        "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n@@ -9,1 +9,1 @@\n-y = 1\n+y = 2\n"
        "--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,1 @@\n-z = 1\n+z = 2\n"
    )
    blocks = PatchParser.parse_unified_diff(diff)
    assert [(b.file_path, b.search_text, b.replace_text) for b in blocks] == [
        ("a.py", "x = 1", "x = 2"), ("a.py", "y = 1", "y = 2"), ("b.py", "z = 1", "z = 2"),
    ]


# --------------------------------------------------------------------------- PATCH-15/16 candidate store / apply


def _save(root: Path, run_id: str, baseline: bytes, new_text: str) -> None:
    CandidateArtifactStore.save_run_artifact(
        run_id=run_id, target_file="calc.py", test_command="t",
        baseline_hash=hashlib.sha256(baseline).hexdigest(), status="SUCCESS", verified=True,
        tier_used=1, attempts=1, cost_usd=0.0, duration_ms=1.0, patched_content=new_text,
        unified_diff="d", workspace_root=root,
    )


def test_run_dir_has_gitignore_and_candidate_bytes_are_exact(tmp_path: Path) -> None:
    root = make_proj(tmp_path / "p")
    new = "a = 1\r\nb = 2\r\n"
    _save(root, "run_x", CALC.encode(), new)
    assert (root / ".argus" / ".gitignore").read_text() == "*\n"
    assert (root / ".argus" / "runs" / "run_x" / "candidate.patched").read_bytes() == new.encode()
    _meta, content = CandidateArtifactStore.load_candidate("run_x", root)
    assert content == new


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_dot_argus_does_not_show_in_git_status(tmp_path: Path) -> None:
    root = make_proj(tmp_path / "p")
    _git(root, "init")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "i")
    out = run_recovery(root, [GOOD])
    assert out.status == "SUCCESS", out.message
    status = subprocess.run(["git", "status", "--porcelain"], cwd=str(root), capture_output=True, text=True).stdout
    assert ".argus" not in status


def test_second_apply_reports_already_applied_once(tmp_path: Path) -> None:
    root = make_proj(tmp_path / "p")
    _save(root, "run_a", CALC.encode(), "def add(a, b):\n    return a + b\n")
    first = AuthoritativePatchApplier.apply("run_a", workspace_root=root)
    assert first.success
    second = AuthoritativePatchApplier.apply("run_a", workspace_root=root)
    assert not second.success and second.error_code == "STALE_TARGET_CONFLICT"
    assert "already applied" in second.message


def test_apply_lock_blocks_concurrent_applier_and_recovers_from_stale_lock(tmp_path: Path) -> None:
    root = make_proj(tmp_path / "p")
    _save(root, "run_l", CALC.encode(), "def add(a, b):\n    return a + b\n")
    lock = root / ".argus" / "runs" / "run_l" / AuthoritativePatchApplier.APPLY_LOCK_NAME
    lock.write_text("123")
    busy = AuthoritativePatchApplier.apply("run_l", workspace_root=root)
    assert not busy.success and busy.error_code == "APPLY_IN_PROGRESS"
    assert (root / "calc.py").read_bytes() == CALC.encode()
    old = lock.stat().st_mtime - 10_000
    os.utime(lock, (old, old))
    ok = AuthoritativePatchApplier.apply("run_l", workspace_root=root)
    assert ok.success and not lock.exists()


def test_concurrent_appliers_report_success_exactly_once(tmp_path: Path) -> None:
    import threading

    root = make_proj(tmp_path / "p")
    _save(root, "run_c", CALC.encode(), "def add(a, b):\n    return a + b\n")
    results: list = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        barrier.wait()
        results.append(AuthoritativePatchApplier.apply("run_c", workspace_root=root))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r.success) == 1
    assert (root / "calc.py").read_bytes() == b"def add(a, b):\n    return a + b\n"


def test_snapshot_size_cap_fails_fast_without_leaking(tmp_path: Path, isolated_tmp: Path) -> None:
    from argus.runtime.workspace import SnapshotTooLargeError

    root = make_proj(tmp_path / "p")
    (root / "big.bin").write_bytes(b"0" * 4096)
    with pytest.raises(SnapshotTooLargeError):
        WorkspaceManager(repo_root=root, target_file="calc.py", snapshot_max_bytes=1024).setup()
    assert not list(isolated_tmp.glob("argus_ws_*"))
    with WorkspaceManager(repo_root=root, target_file="calc.py", snapshot_max_bytes=None) as ws:
        assert (ws.temp_dir / "big.bin").exists()
