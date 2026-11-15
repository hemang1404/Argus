"""Regression tests for the wave-2 integration fixes: controller wiring of the patch/security
changes, config options, provider hardening (antigravity shims, storage) and gateway pricing."""

from __future__ import annotations

import os
import stat
import sys
import types
from pathlib import Path
from typing import List, Optional
from unittest.mock import patch

import pytest

from argus.config import ArgusConfig, UnpricedModelError
from argus.gateway.budget import BudgetGuard, price_for_model
from argus.gateway.config import BudgetConfig
from argus.providers.adapter import CompletionResponse, ConnectionTestResult, ProviderAdapter, ProviderError
from argus.providers.antigravity import AntigravityHostAdapter, find_antigravity_cli
from argus.providers.models import ProviderConfig
from argus.providers.storage import ProviderStorage, get_keyring_service
from argus.runtime.budget import get_gpt_oss_tokenizer
from argus.runtime.controller import RecoveryController


class ScriptedAdapter(ProviderAdapter):
    def __init__(self, responses: List[str], finish_reasons: Optional[List[str]] = None) -> None:
        self.responses = responses
        self.finish_reasons = finish_reasons or []
        self.messages: List[list] = []

    def chat_completion(self, messages, temperature=None, max_tokens=None, effort=None, model=None, **kwargs):
        idx = len(self.messages)
        self.messages.append(messages)
        text = self.responses[idx] if idx < len(self.responses) else "no patch"
        finish = self.finish_reasons[idx] if idx < len(self.finish_reasons) else "stop"
        return CompletionResponse(
            content=text, prompt_tokens=100, completion_tokens=50, duration_ms=1.0,
            model=model or "mock-model", finish_reason=finish,
        )

    def test_connection(self) -> ConnectionTestResult:
        return ConnectionTestResult(True, 200, 1.0, "m", "ok")


def _cfg(**kw) -> ArgusConfig:
    base = dict(provider="ollama", model="mock-model", max_attempts=2, budget_limit_usd=1.0)
    base.update(kw)
    return ArgusConfig(**base)


def _write_project(root: Path, source: str = "def compute_total(a, b):\n    return a + b\n", name: str = "calc.py"):
    (root / name).write_bytes(source.encode("utf-8"))
    (root / "test_calc.py").write_text(
        "from calc import compute_total\n\ndef test_it():\n    assert compute_total(3, 4) == 12\n",
        encoding="utf-8",
    )
    return root / name


def _block(search: str, replace: str) -> str:
    return f"<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE"


# ---------------------------------------------------------------------------
# Controller: typed outcomes instead of tracebacks
# ---------------------------------------------------------------------------

def test_missing_target_is_typed_outcome(tmp_path):
    outcome = RecoveryController(config=_cfg(), adapter=ScriptedAdapter([])).run(
        target_file=tmp_path / "nope.py", test_command="pytest -q", workspace_root=tmp_path
    )
    assert outcome.status == "INVALID_TARGET"
    assert "TargetNotFoundError" in outcome.message


def test_target_outside_workspace_is_typed_outcome(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    target = _write_project(other)
    outcome = RecoveryController(config=_cfg(), adapter=ScriptedAdapter([])).run(
        target_file=target, test_command="pytest -q", workspace_root=project
    )
    assert outcome.status == "INVALID_TARGET"
    assert "TargetOutsideWorkspaceError" in outcome.message


def test_oversized_snapshot_is_config_error_not_traceback(tmp_path):
    target = _write_project(tmp_path)
    (tmp_path / "blob.bin").write_bytes(b"x" * 4096)
    outcome = RecoveryController(config=_cfg(snapshot_max_bytes=1024), adapter=ScriptedAdapter([])).run(
        target_file=target, test_command="pytest -q test_calc.py", workspace_root=tmp_path
    )
    assert outcome.status == "CONFIG_ERROR"
    assert "SnapshotTooLargeError" in outcome.message


def test_undecodable_target_is_typed_outcome(tmp_path):
    target = tmp_path / "calc.py"
    target.write_bytes(b"def compute_total(a, b):\n    return a + b  # caf\xe9\n")
    (tmp_path / "test_calc.py").write_text("def test_it():\n    assert False\n", encoding="utf-8")
    outcome = RecoveryController(config=_cfg(), adapter=ScriptedAdapter([])).run(
        target_file=target, test_command="pytest -q test_calc.py", workspace_root=tmp_path
    )
    assert outcome.status == "INVALID_TARGET"
    assert "UnsupportedEncodingError" in outcome.message


# ---------------------------------------------------------------------------
# Controller: patch wiring
# ---------------------------------------------------------------------------

def test_truncated_block_is_reported_to_the_model(tmp_path):
    target = _write_project(tmp_path)
    truncated = "<<<<<<< SEARCH\n    return a + b\n=======\n    return a *"
    fixed = _block("    return a + b", "    return a * b")
    adapter = ScriptedAdapter([truncated, fixed])
    outcome = RecoveryController(config=_cfg(), adapter=adapter).run(
        target_file=target, test_command="pytest -q test_calc.py", workspace_root=tmp_path
    )
    assert outcome.status == "SUCCESS"
    assert outcome.total_attempts == 2
    # The model was told WHY nothing was applied (not the generic "no recognizable blocks").
    second_prompt = adapter.messages[1][-1]["content"]
    assert "Truncated SEARCH/REPLACE block" in second_prompt
    assert "recognizable" not in outcome.history[0].error_summary
    assert "Malformed SEARCH/REPLACE response" in outcome.history[0].error_summary


def test_attempt_record_separates_patch_tier_from_model_tier(tmp_path):
    target = _write_project(tmp_path)
    truncated = "<<<<<<< SEARCH\n    return a + b\n=======\n    return a *"
    fixed = _block("    return a + b", "    return a * b")
    outcome = RecoveryController(config=_cfg(), adapter=ScriptedAdapter([truncated, fixed])).run(
        target_file=target, test_command="pytest -q test_calc.py", workspace_root=tmp_path
    )
    first, second = outcome.history
    # Nothing was matched on the first attempt: no patch tier, but the model tier is still recorded.
    assert first.tier_used is None and first.model_tier is not None
    # Exact-match edit is patch tier 1; the model ladder index is independent of it.
    assert second.tier_used == 1 and second.model_tier is not None
    assert outcome.tier_used == 1 and outcome.model_tier == second.model_tier


def test_multi_block_patch_gates_only_the_final_state(tmp_path):
    source = "def compute_total(a, b):\n    return a + b\n"
    target = _write_project(tmp_path, source)
    # Block 1 alone leaves an invalid intermediate (`try:` without `except`); the final state is valid.
    reply = "\n".join([
        _block("    return a + b", "    try:\n        return a * b"),
        _block("        return a * b", "        return a * b\n    except TypeError:\n        raise"),
    ])
    outcome = RecoveryController(config=_cfg(), adapter=ScriptedAdapter([reply])).run(
        target_file=target, test_command="pytest -q test_calc.py", workspace_root=tmp_path
    )
    assert outcome.status == "SUCCESS", outcome.message
    assert "except TypeError" in outcome.patched_content


def test_uppercase_py_extension_still_gets_syntax_gate(tmp_path):
    target = tmp_path / "CALC.PY"
    target.write_text("def compute_total(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(
        "import importlib.util, pathlib\n"
        "spec = importlib.util.spec_from_file_location('c', pathlib.Path(__file__).with_name('CALC.PY'))\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "def test_it():\n    assert m.compute_total(3, 4) == 12\n",
        encoding="utf-8",
    )
    bad = _block("    return a + b", "    return a *")
    outcome = RecoveryController(config=_cfg(max_attempts=1), adapter=ScriptedAdapter([bad])).run(
        target_file=target, test_command="pytest -q test_calc.py", workspace_root=tmp_path
    )
    assert outcome.status == "FAILED_MAX_ATTEMPTS"
    assert "syntax" in outcome.history[0].error_summary.lower()
    assert target.read_text(encoding="utf-8") == "def compute_total(a, b):\n    return a + b\n"


def test_enable_tier3_option_reaches_engine():
    assert RecoveryController(config=_cfg(), adapter=ScriptedAdapter([])).patch_engine.enable_tier3 is True
    off = RecoveryController(config=_cfg(enable_tier3=False), adapter=ScriptedAdapter([]))
    assert off.patch_engine.enable_tier3 is False


def test_prompt_scrubbing_keeps_code_but_removes_tokens(tmp_path):
    source = (
        "def compute_total(a, b):\n"
        "    password = compute_secret()\n"
        "    return a + b\n"
    )
    target = _write_project(tmp_path, source)
    leaked = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
    (tmp_path / "test_calc.py").write_text(
        f"from calc import compute_total\n\ndef test_it():\n    assert compute_total(3, 4) == 12, '{leaked}'\n",
        encoding="utf-8",
    )
    adapter = ScriptedAdapter(["nothing"])
    RecoveryController(config=_cfg(max_attempts=1), adapter=adapter).run(
        target_file=target, test_command="pytest -q test_calc.py", workspace_root=tmp_path
    )
    prompt = adapter.messages[0][-1]["content"]
    assert "password = compute_secret()" in prompt  # code the model must see is untouched
    assert leaked not in prompt


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_config_repr_hides_keys():
    secret = "sk-test-0123456789abcdef"
    cfg = ArgusConfig(provider="ollama", model="mock-model", deepseek_api_key=secret)
    assert secret not in repr(cfg)
    pc = ProviderConfig(name="openai", base_url="https://api.openai.com/v1", model="gpt-4o", api_key=secret)
    assert secret not in repr(pc)
    assert secret not in repr(ArgusConfig(provider_config=pc))


def test_snapshot_and_tier3_defaults():
    cfg = ArgusConfig(provider="ollama", model="mock-model")
    assert cfg.enable_tier3 is True
    assert cfg.snapshot_respect_gitignore is True
    assert cfg.snapshot_exclude == []
    assert cfg.snapshot_max_bytes and cfg.snapshot_max_bytes > 0


# ---------------------------------------------------------------------------
# Antigravity: no prompt through cmd.exe, no cwd lookup
# ---------------------------------------------------------------------------

def test_bat_shim_without_companion_is_refused_and_never_executed(tmp_path):
    shim = tmp_path / "agy.bat"
    shim.write_text("@echo off\r\n", encoding="utf-8")
    adapter = AntigravityHostAdapter(cli_path=shim)
    hostile = 'x" & echo I>injected.txt & "'
    with patch("subprocess.run") as run, patch("subprocess.Popen") as popen:
        with pytest.raises(ProviderError) as exc:
            adapter.chat_completion(messages=[{"role": "user", "content": hostile}], cwd=tmp_path)
    assert not run.called and not popen.called
    assert "cmd.exe" in str(exc.value)
    assert not (tmp_path / "injected.txt").exists()
    assert not Path("injected.txt").exists()


def test_bat_shim_with_companion_runs_python_directly(tmp_path):
    shim = tmp_path / "agy.bat"
    shim.write_text("@echo off\r\n", encoding="utf-8")
    (tmp_path / "agy.py").write_text("print('ok')\n", encoding="utf-8")
    adapter = AntigravityHostAdapter(cli_path=shim)
    assert adapter._get_base_cmd() == [sys.executable, str(tmp_path / "agy.py")]


def test_cwd_binary_is_never_discovered(tmp_path, monkeypatch):
    planted = tmp_path / ("agy.exe" if os.name == "nt" else "agy")
    planted.write_bytes(b"")
    if os.name != "nt":
        planted.chmod(0o755)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ARGUS_AGY_BIN", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    # Some platforms (Windows) resolve PATH lookups in the cwd first.
    monkeypatch.setattr("argus.providers.antigravity.shutil.which", lambda name, *a, **k: str(planted))
    assert find_antigravity_cli() is None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class _FakeKeyring(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("keyring")
        self.store: dict = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        self.store.pop((service, name), None)


def test_keyring_service_is_isolated_by_env(tmp_path, monkeypatch):
    fake = _FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.delenv("ARGUS_KEYRING_SERVICE", raising=False)
    assert get_keyring_service() == "argus_ai_inference"
    monkeypatch.setenv("ARGUS_KEYRING_SERVICE", "argus_test_profile")
    store = ProviderStorage(config_dir=tmp_path)
    store.set_api_key("openai", " sk-test-value ")
    assert ("argus_test_profile", "openai") in fake.store
    assert not any(service == "argus_ai_inference" for service, _ in fake.store)
    assert store.get_api_key("openai") == "sk-test-value"


def test_argus_api_key_only_applies_to_custom(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", _FakeKeyring())
    for var in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ARGUS_API_KEY", "custom-endpoint-key")
    store = ProviderStorage(config_dir=tmp_path)
    assert store.get_api_key("custom") == "custom-endpoint-key"
    assert store.get_api_key("openai") is None
    assert store.get_api_key("deepseek") is None
    assert store.get_api_key("some-other-provider") is None


def test_secrets_file_is_created_private(tmp_path, monkeypatch):
    class _Broken(_FakeKeyring):
        def set_password(self, service, name, value):
            raise RuntimeError("no keyring backend")

    monkeypatch.setitem(sys.modules, "keyring", _Broken())
    store = ProviderStorage(config_dir=tmp_path)
    store.set_api_key("openai", "sk-test-value")
    assert store.fallback_secrets_file.is_file()
    assert store.get_api_key("openai") == "sk-test-value"
    if os.name != "nt":
        assert stat.S_IMODE(store.fallback_secrets_file.stat().st_mode) == 0o600
    store.delete_api_key("openai")
    assert store.get_api_key("openai") is None


# ---------------------------------------------------------------------------
# Gateway pricing
# ---------------------------------------------------------------------------

def test_gateway_price_fails_closed_for_unknown_paid_model():
    with pytest.raises(UnpricedModelError):
        price_for_model("brand-new-frontier-9", "openai", "https://api.openai.com/v1")
    assert price_for_model("gpt-4o").input_cost_per_m == 2.50
    assert price_for_model("my-local", "ollama", "http://localhost:11434/v1").input_cost_per_m == 0.0
    assert price_for_model("gemini-9", "antigravity", "host://antigravity-cli").output_cost_per_m == 0.0


def test_gateway_commit_never_guesses_a_low_price_for_unpriced_model():
    guard = BudgetGuard(BudgetConfig())
    ok, _, res_id = guard.reserve("run1", estimated_amount=0.02)
    assert ok
    charged = guard.commit(res_id, "run1", "brand-new-frontier-9", prompt_tokens=10, completion_tokens=10)
    assert charged == pytest.approx(0.02)  # whole reservation counted (as uncertain), not $0.00001


def test_gateway_host_telemetry_uses_record_turn(tmp_path, monkeypatch):
    """The antigravity gateway path used to call a non-existent TelemetryRegistry.record()."""
    from argus.gateway.telemetry import TelemetryRegistry

    assert not hasattr(TelemetryRegistry, "record")
    registry = TelemetryRegistry(workspace_root=tmp_path)
    event = registry.record_turn(
        client="test", requested_model="argus/auto", routed_model="gemini-3.8-flash-low", tier="cheap",
        prompt_tokens=10, completion_tokens=5, latency_ms=1.0, cost_usd=0.0, cost_status="zero_subscription",
    )
    assert event.routed_model == "gemini-3.8-flash-low"
    assert registry.get_summary().total_calls == 1


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

def test_tokenizer_offline_switch_and_cache(monkeypatch):
    monkeypatch.setenv("ARGUS_OFFLINE", "1")
    assert get_gpt_oss_tokenizer() is None
    monkeypatch.delenv("ARGUS_OFFLINE")
    calls = {"n": 0}

    class _Tik(types.ModuleType):
        def get_encoding(self, name):
            calls["n"] += 1
            raise OSError("offline")

    import argus.runtime.budget as budget_mod
    monkeypatch.setitem(sys.modules, "tiktoken", _Tik("tiktoken"))
    monkeypatch.setattr(budget_mod, "_TOKENIZER_CACHE", {})
    assert get_gpt_oss_tokenizer() is None
    first = calls["n"]
    assert first == 3
    assert get_gpt_oss_tokenizer() is None
    assert calls["n"] == first  # a failed download is not retried on every estimate
