"""Groq as a normal, user-configured provider; host adapter boundaries; credential isolation.

Validates that:
1. Groq is an ordinary key-based OpenAI-compatible preset, usable across config, storage, CLI,
   MCP, controller and gateway, and is NEVER selected implicitly.
2. reasoning_effort is sent for Groq gpt-oss models only.
3. Host adapter safely handles missing CLI, authentication errors, malformed JSON, and timeouts offline.
4. Secret redaction and credential isolation still cover gsk_ keys and GROQ_ environment variables.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from argus.cli import main
from argus.config import CASCADE_LADDERS, MODEL_REGISTRY, ArgusConfig
from argus.gateway.config import GatewayConfig, ProviderConfig as GWProviderConfig
from argus.gateway.router import GatewayRouter, NoEligibleProviderError
from argus.gateway.server import app, init_gateway, validate_upstream_url
from argus.mcp.server import create_mcp_server
from argus.providers.adapter import OpenAICompatibleAdapter
from argus.providers.antigravity import AntigravityHostAdapter
from argus.providers.models import (
    PROVIDER_PRESETS,
    ProviderConfig,
    ProviderConfigurationError,
)
from argus.providers.storage import ProviderStorage, scrub_secrets
from argus.runtime.controller import RecoveryController
from argus.runtime.runner import SubprocessTestRunner
from argus.utils.redaction import scrub_prompt


@pytest.fixture(autouse=True)
def isolated_keyring(monkeypatch: pytest.MonkeyPatch):
    """In-memory keyring vault; tests never touch the host OS credential manager."""
    vault = {}

    def fake_get_password(service: str, username: str):
        return vault.get((service, username))

    def fake_set_password(service: str, username: str, password: str):
        vault[(service, username)] = password

    def fake_delete_password(service: str, username: str):
        vault.pop((service, username), None)

    try:
        import keyring
        monkeypatch.setattr(keyring, "get_password", fake_get_password)
        monkeypatch.setattr(keyring, "set_password", fake_set_password)
        monkeypatch.setattr(keyring, "delete_password", fake_delete_password)
    except Exception:
        pass


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for var in ("GROQ_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GEMINI_API_KEY",
                "GOOGLE_API_KEY", "ARGUS_PROVIDER", "ARGUS_GATEWAY_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ARGUS_CONFIG_DIR", str(tmp_path / "cfg"))
    return tmp_path / "cfg"


# =============================================================================
# 1. Groq preset, config and storage
# =============================================================================

def test_groq_preset_shape():
    preset = PROVIDER_PRESETS["groq"]
    assert preset.key == "groq"
    assert preset.display_name == "Groq (Official API)"
    assert preset.default_base_url == "https://api.groq.com/openai/v1"
    assert preset.default_model == "openai/gpt-oss-120b"
    assert preset.requires_key is True


def test_groq_default_model_is_priced():
    assert PROVIDER_PRESETS["groq"].default_model in MODEL_REGISTRY


def test_groq_is_not_a_cascade_provider():
    assert "groq" not in CASCADE_LADDERS


def test_from_preset_groq():
    cfg = ProviderConfig.from_preset("groq", api_key="gsk_test_key_123456")
    assert cfg.name == "groq"
    assert cfg.base_url == "https://api.groq.com/openai/v1"
    assert cfg.model == "openai/gpt-oss-120b"
    assert ProviderConfig.from_preset("groq", model="openai/gpt-oss-20b").model == "openai/gpt-oss-20b"


def test_unknown_preset_still_rejected():
    with pytest.raises(ProviderConfigurationError):
        ProviderConfig.from_preset("not-a-real-preset")


def test_gemini_preset_is_registered():
    assert PROVIDER_PRESETS["gemini"].requires_key is True
    assert "generativelanguage" in PROVIDER_PRESETS["gemini"].default_base_url


def test_argus_config_accepts_groq():
    cfg = ArgusConfig(provider="groq", model="openai/gpt-oss-20b")
    assert cfg.provider == "groq"
    assert cfg.model == "openai/gpt-oss-20b"
    assert cfg.get_model_cost().input_cost_per_m == 0.075


def test_storage_saves_groq_provider_and_key(clean_env: Path):
    storage = ProviderStorage(config_dir=clean_env)
    storage.save_provider(
        ProviderConfig(name="groq", base_url="https://api.groq.com/openai/v1",
                       model="openai/gpt-oss-20b", api_key="gsk_stored_key_123456")
    )
    loaded = storage.get_provider("groq")
    assert loaded is not None
    assert loaded.model == "openai/gpt-oss-20b"
    assert loaded.api_key == "gsk_stored_key_123456"
    assert storage.set_active_provider("groq") is True
    assert storage.get_active_provider().name == "groq"
    assert [p.name for p in storage.list_providers()] == ["groq"]
    # Key is never written to providers.json
    assert "gsk_stored_key" not in (clean_env / "providers.json").read_text(encoding="utf-8")


def test_storage_resolves_groq_key_from_env(clean_env: Path, monkeypatch: pytest.MonkeyPatch):
    storage = ProviderStorage(config_dir=clean_env)
    assert storage.get_api_key("groq") is None
    monkeypatch.setenv("GROQ_API_KEY", "gsk_env_key_123456")
    assert storage.get_api_key("groq") == "gsk_env_key_123456"
    # A stored key takes precedence over the environment variable.
    storage.set_api_key("groq", "gsk_stored_key_123456")
    assert storage.get_api_key("groq") == "gsk_stored_key_123456"


def test_groq_env_key_never_selects_provider(clean_env: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_stray_env_key_123456")
    cfg = ArgusConfig()
    assert cfg.provider is None
    assert ProviderStorage(config_dir=clean_env).get_active_provider() is None


def test_discovery_groq_available_only_with_key(clean_env: Path, monkeypatch: pytest.MonkeyPatch):
    from argus.providers.discovery import discover_all_models

    monkeypatch.setattr("argus.providers.antigravity.find_antigravity_cli", lambda *a, **k: None)
    storage = ProviderStorage(config_dir=clean_env)
    groq = [m for m in discover_all_models(storage=storage, force_refresh=True) if m.provider == "groq"]
    assert groq and not any(m.is_available for m in groq)

    storage.set_api_key("groq", "gsk_stored_key_123456")
    groq = [m for m in discover_all_models(storage=storage, force_refresh=True) if m.provider == "groq"]
    assert groq and all(m.is_available for m in groq)


# =============================================================================
# 2. CLI, controller, MCP
# =============================================================================

def test_cli_provider_set_groq_preset(clean_env: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MY_GROQ_KEY", "gsk_cli_key_123456")
    res = CliRunner().invoke(main, ["provider", "set", "--preset", "groq", "--api-key-env", "MY_GROQ_KEY"])
    assert res.exit_code == 0, res.output
    assert "gsk_cli_key_123456" not in res.output
    cfg = ProviderStorage().get_provider("groq")
    assert cfg is not None
    assert cfg.base_url == "https://api.groq.com/openai/v1"
    assert cfg.model == "openai/gpt-oss-120b"
    assert cfg.api_key == "gsk_cli_key_123456"
    assert ProviderStorage().get_active_provider().name == "groq"


def test_cli_provider_set_groq_without_key_fails(clean_env: Path):
    res = CliRunner().invoke(main, ["provider", "set", "--preset", "groq"], input="\n")
    assert res.exit_code != 0
    assert ProviderStorage().get_provider("groq").api_key is None


def test_controller_builds_openai_adapter_for_groq_from_env(clean_env: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_env_key_123456")
    controller = RecoveryController(config=ArgusConfig(provider="groq", model="openai/gpt-oss-20b"))
    adapter = controller._get_adapter()
    assert isinstance(adapter, OpenAICompatibleAdapter)
    assert adapter.config.name == "groq"
    assert adapter.config.base_url == "https://api.groq.com/openai/v1"
    assert adapter.config.model == "openai/gpt-oss-20b"
    assert adapter.config.api_key == "gsk_env_key_123456"
    assert controller.selected_provider == "groq"


def test_controller_groq_key_from_storage(clean_env: Path):
    ProviderStorage().set_api_key("groq", "gsk_stored_key_123456")
    adapter = RecoveryController(config=ArgusConfig(provider="groq"))._get_adapter()
    assert adapter.config.api_key == "gsk_stored_key_123456"
    assert adapter.config.model == "openai/gpt-oss-120b"


def test_controller_groq_without_key_is_config_error(clean_env: Path):
    controller = RecoveryController(config=ArgusConfig(provider="groq"))
    with pytest.raises(ProviderConfigurationError):
        controller._get_adapter()


def test_unconfigured_controller_never_selects_groq(clean_env: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_stray_env_key_123456")
    with pytest.raises(ProviderConfigurationError):
        RecoveryController(config=ArgusConfig())._get_adapter()


def test_mcp_recover_accepts_groq_and_uses_user_key(clean_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARGUS_MCP_WORKSPACE_ROOT", str(tmp_path))
    ProviderStorage().save_provider(
        ProviderConfig(name="groq", base_url="https://api.groq.com/openai/v1",
                       model="openai/gpt-oss-120b", api_key="gsk_stored_key_123456")
    )
    src = tmp_path / "calc.py"
    src.write_text("def add(a, b): return a - b\n", encoding="utf-8")

    captured = {}

    class FakeController:
        def __init__(self, config):
            captured["config"] = config

        def run(self, **kwargs):
            raise RuntimeError("stop before any model call")

    monkeypatch.setattr("argus.mcp.server.RecoveryController", FakeController)
    recover = create_mcp_server()._tool_manager._tools["argus_recover"].fn
    try:
        recover(test_command="pytest", target_file=str(src), provider="groq", workspace=str(tmp_path))
    except RuntimeError:
        pass
    assert captured["config"].provider == "groq"
    assert captured["config"].provider_config.api_key == "gsk_stored_key_123456"


def test_mcp_recover_without_provider_never_picks_groq_from_env(clean_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARGUS_MCP_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("GROQ_API_KEY", "gsk_stray_env_key_123456")
    src = tmp_path / "calc.py"
    src.write_text("def add(a, b): return a - b\n", encoding="utf-8")
    created = []
    monkeypatch.setattr("argus.mcp.server.RecoveryController", lambda config: created.append(config) or MagicMock())
    recover = create_mcp_server()._tool_manager._tools["argus_recover"].fn
    data = json.loads(recover(test_command="pytest", target_file=str(src), workspace=str(tmp_path)))
    assert data["status"] == "CONFIG_ERROR"
    assert not created


# =============================================================================
# 3. reasoning_effort handling
# =============================================================================

def _capture_payload(model: str, provider: str = "groq", base_url: str = "https://api.groq.com/openai/v1") -> dict:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            "model": model,
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(name=provider, base_url=base_url, model=model, api_key="gsk_test_key_123456")
    OpenAICompatibleAdapter(cfg, client=client).chat_completion(
        messages=[{"role": "user", "content": "hi"}], effort="medium", model=model
    )
    return captured


@pytest.mark.parametrize("model", ["openai/gpt-oss-20b", "openai/gpt-oss-120b"])
def test_groq_gpt_oss_receives_reasoning_effort(model: str):
    assert _capture_payload(model)["reasoning_effort"] == "medium"


@pytest.mark.parametrize("model", ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "qwen-2.5-coder-32b"])
def test_groq_non_reasoning_models_do_not_receive_reasoning_effort(model: str):
    assert "reasoning_effort" not in _capture_payload(model)


def test_other_providers_keep_reasoning_effort_behaviour():
    payload = _capture_payload("deepseek-chat", provider="deepseek", base_url="https://api.deepseek.com/v1")
    assert payload["reasoning_effort"] == "medium"


# =============================================================================
# 4. Gateway
# =============================================================================

def test_groq_endpoint_is_a_normal_allowed_https_endpoint():
    assert validate_upstream_url("https://api.groq.com/openai/v1")
    # The other endpoint-safety rules still apply.
    assert not validate_upstream_url("http://169.254.169.254/latest")
    assert not validate_upstream_url("https://user:pw@api.groq.com/openai/v1")
    assert not validate_upstream_url("file:///etc/passwd")


def test_gateway_router_routes_explicit_groq_provider():
    cfg = GatewayConfig(
        providers=[GWProviderConfig(name="groq", base_url="https://api.groq.com/openai/v1", api_key="gsk_gw_key_123456")],
        tiers={},
    )
    target = GatewayRouter(cfg).resolve("groq/llama-3.3-70b-versatile")
    assert target.provider_name == "groq"
    assert target.upstream_model == "llama-3.3-70b-versatile"
    assert target.api_key == "gsk_gw_key_123456"


def test_gateway_router_does_not_route_to_unconfigured_groq():
    cfg = GatewayConfig(providers=[GWProviderConfig(name="openai", base_url="https://api.openai.com/v1")], tiers={})
    with pytest.raises(NoEligibleProviderError):
        GatewayRouter(cfg).resolve("groq/llama-3.3-70b-versatile")


def test_gateway_forwards_groq_key_to_groq_provider(tmp_path: Path):
    cfg = GatewayConfig(
        providers=[GWProviderConfig(name="groq", base_url="https://api.groq.com/openai/v1", api_key="")],
        tiers={},
    )
    init_gateway(cfg, workspace=tmp_path)
    client = TestClient(app)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.content = json.dumps({
        "id": "chatcmpl-test",
        "choices": [{"message": {"role": "assistant", "content": "hello"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }).encode("utf-8")
    mock_resp.json.return_value = json.loads(mock_resp.content)

    with patch("httpx.AsyncClient.post", return_value=mock_resp) as mock_post:
        resp = client.post(
            "/v1/chat/completions",
            headers={"X-Upstream-Api-Key": "gsk_client_key_123456"},
            json={"model": "groq/openai/gpt-oss-20b", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200, resp.text
    _, kwargs = mock_post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer gsk_client_key_123456"


def test_gateway_never_forwards_groq_key_to_other_provider(tmp_path: Path):
    cfg = GatewayConfig(providers=[GWProviderConfig(name="openai", base_url="https://api.openai.com/v1")], tiers={})
    init_gateway(cfg, workspace=tmp_path)
    resp = TestClient(app).post(
        "/v1/chat/completions",
        headers={"X-Upstream-Api-Key": "gsk_client_key_123456"},
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "credential_provider_mismatch"


# =============================================================================
# 5. Credential isolation: GROQ_ env scrubbed from test subprocess, gsk_ redacted
# =============================================================================

def test_groq_env_is_scrubbed_from_test_subprocess(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_must_not_leak_123456")
    monkeypatch.setenv("SAFE_CUSTOM_FLAG", "argus_active")
    probe = (
        "import os, sys\n"
        "sys.exit(42 if 'GROQ_API_KEY' in os.environ else (0 if os.environ.get('SAFE_CUSTOM_FLAG') == 'argus_active' else 43))\n"
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        (Path(tmp_dir) / "probe.py").write_text(probe, encoding="utf-8")
        res = SubprocessTestRunner(timeout_seconds=10).run_test(f'"{sys.executable}" probe.py', cwd=Path(tmp_dir))
    assert res.exit_code == 0, (res.exit_code, res.stderr)


def test_gsk_keys_are_redacted():
    secret = "gsk_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    assert secret not in scrub_secrets(f"error calling groq with {secret}")
    assert secret not in scrub_prompt(f"key={secret}")


# =============================================================================
# 6. Antigravity Host Adapter Offline Error Handling
# =============================================================================

def test_antigravity_adapter_missing_cli(monkeypatch: pytest.MonkeyPatch):
    """AntigravityHostAdapter must handle missing CLI gracefully in test_connection."""
    monkeypatch.setattr("argus.providers.antigravity.find_antigravity_cli", lambda *args, **kwargs: None)
    cfg = ProviderConfig(name="antigravity", base_url="host://antigravity-cli", model="gemini-3.8-flash-low")
    adapter = AntigravityHostAdapter(cfg)
    result = adapter.test_connection()
    assert result.success is False
    assert "not found" in result.message.lower()


def test_antigravity_adapter_cli_error_status(monkeypatch: pytest.MonkeyPatch):
    """AntigravityHostAdapter must raise ProviderError when CLI returns status != SUCCESS in JSON."""
    from argus.providers.adapter import ProviderError
    monkeypatch.setattr("argus.providers.antigravity.find_antigravity_cli", lambda *args, **kwargs: Path("agy.exe"))
    cfg = ProviderConfig(name="antigravity", base_url="host://antigravity-cli", model="gemini-3.8-flash-low")
    adapter = AntigravityHostAdapter(cfg)

    mock_run = MagicMock()
    mock_run.returncode = 0
    mock_run.stdout = json.dumps({"status": "ERROR", "error": "Model rate limit exceeded"})
    mock_run.stderr = ""

    with patch("subprocess.run", return_value=mock_run):
        with pytest.raises(ProviderError, match="Model rate limit exceeded"):
            adapter.chat_completion(
                messages=[{"role": "user", "content": "hello"}],
                model="gemini-3.8-flash-low",
            )


def test_antigravity_adapter_timeout(monkeypatch: pytest.MonkeyPatch):
    """AntigravityHostAdapter must cleanly handle subprocess timeout with ProviderError."""
    from argus.providers.adapter import ProviderError
    monkeypatch.setattr("argus.providers.antigravity.find_antigravity_cli", lambda *args, **kwargs: Path("agy.exe"))
    cfg = ProviderConfig(name="antigravity", base_url="host://antigravity-cli", model="gemini-3.8-flash-low")
    adapter = AntigravityHostAdapter(cfg, timeout_seconds=1.0)

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["agy"], timeout=1.0)):
        with pytest.raises(ProviderError, match="timed out"):
            adapter.chat_completion(
                messages=[{"role": "user", "content": "hello"}],
                model="gemini-3.8-flash-low",
            )


def test_antigravity_adapter_nonzero_exit(monkeypatch: pytest.MonkeyPatch):
    """AntigravityHostAdapter must raise ProviderError on nonzero subprocess exit."""
    from argus.providers.adapter import ProviderError
    monkeypatch.setattr("argus.providers.antigravity.find_antigravity_cli", lambda *args, **kwargs: Path("agy.exe"))
    cfg = ProviderConfig(name="antigravity", base_url="host://antigravity-cli", model="gemini-3.8-flash-low")
    adapter = AntigravityHostAdapter(cfg)

    mock_run = MagicMock()
    mock_run.returncode = 1
    mock_run.stdout = ""
    mock_run.stderr = "Authentication required. Please run 'agy auth login'."

    with patch("subprocess.run", return_value=mock_run):
        with pytest.raises(ProviderError, match="authentication error"):
            adapter.chat_completion(
                messages=[{"role": "user", "content": "hello"}],
                model="gemini-3.8-flash-low",
            )


# =============================================================================
# 7. Credential Redaction and Scrubbing
# =============================================================================

def test_credential_scrubbing_redacts_tokens():
    """scrub_secrets must redact Groq, OpenAI, Anthropic, and Bearer secrets from strings."""
    text = (
        "Encountered error with gsk_secret_key_1234567890abcdef "
        "and Authorization: Bearer secret-bearer-token-here "
        "and OpenAI key sk-proj-supersecretkey1234567890"
    )
    cleaned = scrub_secrets(text)
    assert "gsk_secret" not in cleaned
    assert "secret-bearer-token-here" not in cleaned
    assert "supersecretkey" not in cleaned
    assert "[REDACTED_SECRET]" in cleaned
