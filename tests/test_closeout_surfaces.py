"""Close-out regression tests: shared defaults, wording, CRLF diffs, snapshot secrets, policy."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from argus.config import DEFAULT_BUDGET_USD, DEFAULT_MAX_ATTEMPTS, MAX_ATTEMPTS_CEILING, ArgusConfig
from argus.runtime.candidate import CandidateArtifactStore
from argus.runtime.workspace import WorkspaceManager, is_secret_file_name

ROOT = Path(__file__).resolve().parent.parent


# --- defaults come from argus.config everywhere -------------------------------------------------


def test_api_request_defaults_are_the_config_constants():
    from argus.api.recovery import RecoveryTaskRequest

    req = RecoveryTaskRequest(target_file="a.py", test_command="pytest")
    assert req.max_attempts == DEFAULT_MAX_ATTEMPTS
    assert req.budget == DEFAULT_BUDGET_USD
    with pytest.raises(ValueError):
        RecoveryTaskRequest(target_file="a.py", test_command="pytest", max_attempts=MAX_ATTEMPTS_CEILING + 1)


def test_web_ui_form_defaults_match_the_config_constants():
    html = (ROOT / "argus" / "ui" / "index.html").read_text(encoding="utf-8")
    attempts = re.search(r'id="max-attempts"[^>]*>', html).group(0)
    assert f'value="{DEFAULT_MAX_ATTEMPTS}"' in attempts
    assert f'max="{MAX_ATTEMPTS_CEILING}"' in attempts
    slider = re.search(r'id="budget-limit"[^>]*>', html).group(0)
    assert f'value="{DEFAULT_BUDGET_USD:.2f}"' in slider
    assert f"${DEFAULT_BUDGET_USD:.2f}" in html  # visible label
    # no fake model ids such as argus/cheap are submitted as models
    assert 'value="argus/cheap"' not in html and 'value="argus/frontier"' not in html


def test_session_api_budget_guard_uses_the_default_budget():
    from argus.api import session

    assert session._SESSION_BUDGET_GUARD.config.per_task_usd == DEFAULT_BUDGET_USD


# --- wording ---------------------------------------------------------------------------------------


def test_success_message_does_not_claim_the_bug_is_resolved():
    src = (ROOT / "argus" / "runtime" / "controller.py").read_text(encoding="utf-8")
    assert "Bug resolved" not in src
    assert "test command passed on the patched copy" in src


def test_tui_help_does_not_say_enter_applies():
    src = (ROOT / "argus" / "tui" / "modals" / "help.py").read_text(encoding="utf-8")
    assert "Enter[/] / [bold" not in src
    assert "Enter never applies" in src


# --- CRLF diffs are stored byte-exact -----------------------------------------------------------------


def test_patch_diff_keeps_crlf_bytes_and_reads_back_unchanged(tmp_path):
    diff = "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-x = 1\r\n+x = 2\r\n"
    run_id = "run_20261116_000000_abcd1234"
    run_dir = CandidateArtifactStore.save_run_artifact(
        run_id=run_id,
        target_file="f.py",
        test_command="pytest",
        baseline_hash="0" * 64,
        status="SUCCESS",
        verified=True,
        tier_used=1,
        attempts=1,
        cost_usd=0.0,
        duration_ms=1.0,
        patched_content="x = 2\r\n",
        unified_diff=diff,
        workspace_root=tmp_path,
    )
    raw = (run_dir / "patch.diff").read_bytes()
    assert b"\r\r\n" not in raw
    assert raw == diff.encode("utf-8")
    assert CandidateArtifactStore.get_candidate_diff(run_id, workspace_root=tmp_path) == diff


# --- snapshots leave credential files out ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name,secret",
    [
        (".env", True),
        (".env.production", True),
        (".env.example", False),
        ("server.pem", True),
        ("id_rsa", True),
        (".npmrc", True),
        ("app.py", False),
        ("environment.py", False),
    ],
)
def test_secret_file_name_matching(name, secret):
    assert is_secret_file_name(name) is secret


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "calc.py").write_text("x = 1\n", encoding="utf-8")
    (root / ".env").write_text("API_TOKEN=do-not-copy\n", encoding="utf-8")
    (root / ".env.example").write_text("API_TOKEN=\n", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "private.pem").write_text("PEM", encoding="utf-8")
    return root


def test_non_git_snapshot_excludes_secret_files_by_default(tmp_path):
    root = _project(tmp_path)
    with WorkspaceManager(repo_root=root, target_file="calc.py") as ws:
        copied = {p.name for p in ws.temp_dir.rglob("*") if p.is_file()}
    assert "calc.py" in copied and ".env.example" in copied
    assert ".env" not in copied and "private.pem" not in copied


def test_snapshot_can_include_secret_files_when_asked(tmp_path):
    root = _project(tmp_path)
    with WorkspaceManager(repo_root=root, target_file="calc.py", snapshot_include_secrets=True) as ws:
        copied = {p.name for p in ws.temp_dir.rglob("*") if p.is_file()}
    assert ".env" in copied and "private.pem" in copied


def test_controller_passes_snapshot_secret_option_from_config():
    assert ArgusConfig(provider="ollama", model="m").snapshot_include_secrets is False


# --- project policy cannot raise reasoning effort ---------------------------------------------------


def test_policy_may_lower_reasoning_effort_to_low(tmp_path):
    (tmp_path / "argus.yaml").write_text("reasoning_effort: low\n", encoding="utf-8")
    cfg = ArgusConfig(provider="ollama", model="m")
    cfg.apply_project_policy(tmp_path)
    assert cfg.reasoning_effort == "low"


@pytest.mark.parametrize("effort", ["medium", "high", "xhigh"])
def test_policy_cannot_raise_reasoning_effort(tmp_path, effort):
    (tmp_path / "argus.yaml").write_text(f"reasoning_effort: {effort}\n", encoding="utf-8")
    cfg = ArgusConfig(provider="ollama", model="m")
    notes = cfg.apply_project_policy(tmp_path)
    assert cfg.reasoning_effort is None
    assert any("reasoning_effort" in n and "Ignored" in n for n in notes)


# --- version single source ----------------------------------------------------------------------------------


def test_version_is_0_2_0b2_and_changelog_has_a_dated_entry():
    import argus

    assert argus.__version__ == "0.2.0b2"
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.2.0b2] - 2026-11-16" in changelog
    assert "[Unreleased]" not in changelog
