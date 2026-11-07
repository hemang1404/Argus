"""Exhaustive Offline Tests for Groq Prohibition, Host Adapter Boundaries, and Credential Isolation.

Validates that:
1. Groq is prohibited across all execution paths (models, presets, storage, router, gateway, transport, controller, harness).
2. Host adapter safely handles missing CLI, authentication errors, malformed JSON, and timeouts offline.
3. Secret redaction and credential isolation prevent leakage of keys and bearer tokens.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpcore
import pytest
from starlette.testclient import TestClient

from argus.config import ArgusConfig
from argus.providers.adapter import OpenAICompatibleAdapter
from argus.gateway.config import GatewayConfig, ProviderConfig as GWProviderConfig, TierDefinition
from argus.gateway.router import GatewayRouter, NoEligibleProviderError
from argus.gateway.server import app, init_gateway, validate_upstream_url
from argus.gateway.transport import SafeAnyIOBackend
from argus.providers.antigravity import AntigravityHostAdapter
from argus.providers.models import (
    PROVIDER_PRESETS,
    ProviderConfig,
)
from argus.providers.storage import ProviderStorage, scrub_secrets
from argus.runtime.controller import RecoveryController


@pytest.fixture(autouse=True)
def isolated_keyring(monkeypatch: pytest.MonkeyPatch):
    """Ensure provider tests use an in-memory keyring vault and never mutate the host OS credential manager."""
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


# =============================================================================
# 1. Groq Prohibition and Gemini Support Across Provider Models, Presets, and Storage
# =============================================================================

def test_groq_preset_is_strictly_prohibited():
    """Groq preset must NOT exist in PROVIDER_PRESETS."""
    assert "groq" not in PROVIDER_PRESETS


def test_gemini_preset_is_registered():
    """Gemini preset must exist in PROVIDER_PRESETS with valid OpenAI-compatible endpoint."""
    assert "gemini" in PROVIDER_PRESETS
    assert PROVIDER_PRESETS["gemini"].requires_key is True
    assert "generativelanguage" in PROVIDER_PRESETS["gemini"].default_base_url


def test_provider_config_from_preset_rejects_groq():
    """ProviderConfig.from_preset must raise ValueError when groq is requested."""
    with pytest.raises(ValueError, match="strictly prohibited"):
        ProviderConfig.from_preset("groq")


def test_provider_config_from_preset_supports_gemini():
    """ProviderConfig.from_preset creates valid config for gemini."""
    cfg_gem = ProviderConfig.from_preset("gemini", api_key="AIzaSyTest")
    assert cfg_gem.name == "gemini"
    assert cfg_gem.api_key == "AIzaSyTest"


def test_storage_rejects_groq_provider(tmp_path: Path):
    """ProviderStorage.save_provider must refuse to save a groq configuration."""
    storage = ProviderStorage(config_dir=tmp_path)
    cfg = ProviderConfig(name="groq", base_url="https://api.groq.com/openai/v1", model="openai/gpt-oss-20b", api_key="gsk-test")
    with pytest.raises(ValueError, match="strictly prohibited"):
        storage.save_provider(cfg)


def test_storage_rejects_groq_api_key(tmp_path: Path):
    """ProviderStorage.set_api_key must refuse groq api key."""
    storage = ProviderStorage(config_dir=tmp_path)
    with pytest.raises(ValueError, match="strictly prohibited"):
        storage.set_api_key("groq", "gsk-test-secret-12345")


def test_storage_supports_gemini_provider_and_key(tmp_path: Path):
    """ProviderStorage must save gemini configuration and key."""
    storage = ProviderStorage(config_dir=tmp_path)
    cfg_gem = ProviderConfig(name="gemini", base_url="https://generativelanguage.googleapis.com/v1beta/openai", model="gemini-2.5-flash", api_key="AIzaSyTest")
    storage.save_provider(cfg_gem)
    loaded_gem = storage.get_provider("gemini")
    assert loaded_gem is not None
    assert loaded_gem.name == "gemini"
    assert storage.set_api_key("gemini", "AIzaSyTestSecret12345") is True


def test_groq_never_silently_selected_via_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Presence of GROQ_API_KEY in environment must never cause Argus to select Groq."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk-malicious-fallback-key")
    monkeypatch.setenv("ARGUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ARGUS_PROVIDER", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    cfg = ArgusConfig()
    assert cfg.provider != "groq", f"Silent Groq selection detected: provider is {cfg.provider}"
    assert not hasattr(cfg, "groq_api_key") or getattr(cfg, "groq_api_key") is None


def test_argus_config_refuses_groq():
    """Explicitly configuring groq provider in ArgusConfig must raise ValueError."""
    with pytest.raises(ValueError, match="strictly prohibited"):
        ArgusConfig(provider="groq")


def test_recovery_controller_rejects_groq():
    """RecoveryController must reject groq provider in _get_adapter with RuntimeError."""
    cfg = ArgusConfig()
    cfg.provider = "custom"  # bypass __post_init__ check to test controller-level defense
    controller = RecoveryController(config=cfg)
    controller.config.provider = "groq"
    with pytest.raises(RuntimeError, match="strictly prohibited"):
        controller._get_adapter()


def test_recovery_controller_supports_gemini_with_key(monkeypatch: pytest.MonkeyPatch):
    """RecoveryController must successfully initialize adapter when gemini key is provided."""
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyTestValidKey")
    cfg = ArgusConfig(provider="gemini")
    controller = RecoveryController(config=cfg)
    adapter = controller._get_adapter()
    assert isinstance(adapter, OpenAICompatibleAdapter)
    assert adapter.config.name == "gemini"
    assert "generativelanguage" in adapter.config.base_url


# =============================================================================
# 3. Groq Prohibition at Gateway Router, SSRF Boundary, and Transport
# =============================================================================

def test_router_rejects_groq_model():
    """GatewayRouter must reject any groq model string with NoEligibleProviderError."""
    p_openai = GWProviderConfig(name="openai", base_url="https://api.openai.com/v1")
    cfg = GatewayConfig(providers=[p_openai], tiers={})
    router = GatewayRouter(cfg)

    with pytest.raises(NoEligibleProviderError, match="Groq is strictly prohibited"):
        router.resolve("groq")

    with pytest.raises(NoEligibleProviderError, match="Groq is strictly prohibited"):
        router.resolve("groq/llama-3.3-70b-versatile")

    with pytest.raises(NoEligibleProviderError, match="Groq is strictly prohibited"):
        router.resolve("https://api.groq.com/openai/v1")


def test_ssrf_validator_blocks_groq_domains():
    """validate_upstream_url must strictly block api.groq.com and any .groq.com hostname."""
    assert not validate_upstream_url("https://api.groq.com/openai/v1")
    assert not validate_upstream_url("https://api.groq.com/v1")
    assert not validate_upstream_url("https://subdomain.groq.com/v1")
    assert not validate_upstream_url("http://groq.com/api")


@pytest.mark.asyncio
async def test_safe_transport_connect_tcp_blocks_groq():
    """SafeAnyIOBackend.connect_tcp must fail closed if connection to groq.com is attempted."""
    backend = SafeAnyIOBackend()
    with pytest.raises(httpcore.ConnectError, match="Groq endpoints are strictly prohibited"):
        await backend.connect_tcp(host="api.groq.com", port=443)

    with pytest.raises(httpcore.ConnectError, match="Groq endpoints are strictly prohibited"):
        await backend.connect_tcp(host="custom.groq.com", port=443)


def test_gateway_server_rejects_groq_headers_and_keys(tmp_path: Path):
    """Gateway HTTP endpoint must reject groq provider headers and gsk_ key signatures."""
    p_openai = GWProviderConfig(name="openai", base_url="https://api.openai.com/v1")
    cfg = GatewayConfig(providers=[p_openai], tiers={})
    init_gateway(cfg, workspace=tmp_path)
    client = TestClient(app)

    # 1. Prohibited X-Upstream-Provider
    resp1 = client.post(
        "/v1/chat/completions",
        headers={"X-Upstream-Provider": "groq"},
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp1.status_code == 400
    assert resp1.json()["error"]["code"] == "prohibited_provider"

    # 2. Prohibited gsk_ Bearer token
    resp2 = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer gsk_some_prohibited_key"},
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp2.status_code == 400
    assert resp2.json()["error"]["code"] == "prohibited_provider"


# =============================================================================
# 4. Legacy Harness Fails Closed
# =============================================================================

def test_src_groq_client_fails_closed():
    """src.groq_client.GroqClient must raise RuntimeError upon instantiation or be absent."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    try:
        from src.groq_client import GroqClient
        with pytest.raises(RuntimeError, match="Groq is strictly prohibited"):
            GroqClient()
    except (ImportError, ModuleNotFoundError):
        pass  # Legacy src/ package cleanly removed from release distribution


# =============================================================================
# 5. Antigravity Host Adapter Offline Error Handling
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
# 6. Credential Redaction and Scrubbing
# =============================================================================

def test_credential_scrubbing_redacts_tokens():
    """scrub_secrets must redact Groq, OpenAI, Anthropic, and Bearer secrets from strings and dicts."""
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
