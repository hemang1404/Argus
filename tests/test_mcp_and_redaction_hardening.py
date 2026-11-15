"""MCP trust boundary (workspace/budget from an LLM caller) and prompt-safe redaction."""

from __future__ import annotations

import pytest

from argus.mcp.server import DEFAULT_MAX_BUDGET_USD, clamp_mcp_budget, resolve_mcp_workspace
from argus.utils.redaction import scrub_prompt, scrub_secrets


def test_workspace_must_stay_inside_server_root(tmp_path):
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    assert resolve_mcp_workspace(None, root) == root
    assert resolve_mcp_workspace(str(root / "sub"), root) == root / "sub"
    with pytest.raises(ValueError):
        resolve_mcp_workspace(str(tmp_path), root)
    with pytest.raises(ValueError):
        resolve_mcp_workspace(str(root / ".." / "other"), root)


def test_budget_is_capped_and_validated(monkeypatch):
    monkeypatch.delenv("ARGUS_MCP_MAX_BUDGET_USD", raising=False)
    assert clamp_mcp_budget(0.05) == 0.05
    assert clamp_mcp_budget(100.0) == DEFAULT_MAX_BUDGET_USD
    for bad in (float("nan"), float("inf"), 0, -1, "abc"):
        with pytest.raises(ValueError):
            clamp_mcp_budget(bad)
    monkeypatch.setenv("ARGUS_MCP_MAX_BUDGET_USD", "1.0")
    assert clamp_mcp_budget(100.0) == 1.0


def test_scrub_prompt_preserves_code_semantics():
    code = (
        "def login(password=None, api_key=None):\n"
        "    password = get_password()\n"
        "    headers = {'api_key': cfg.key}\n"
        "    return connect('postgres://user:secret@host/db', token=password)\n"
    )
    assert scrub_prompt(code) == code
    # the log/artifact scrubber is intentionally more aggressive
    assert scrub_secrets(code) != code


def test_scrub_prompt_still_removes_token_shaped_secrets():
    key = "sk-" + "a" * 32
    pem = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
    out = scrub_prompt(f'K = "{key}"\n{pem}\nmine=hunter2xyz', keys=["hunter2xyz"])
    assert key not in out and "abc" not in out and "hunter2xyz" not in out
