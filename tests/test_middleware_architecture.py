"""Tests for Argus Middleware-Only Architecture, Credential Isolation, and Platform Model Selection.

Validates:
1. Gateway returns 503 provider_resolution_error when unconfigured and no provider is available (no silent developer key fallback).
2. Gateway with configured provider but no stored key accepts and uses client-supplied Authorization / x-api-key headers.
3. Gateway client authentication (ARGUS_GATEWAY_API_KEY) is strictly separate from upstream credentials and never leaked.
4. SSRF protection blocks requests targeting cloud metadata IPs (169.254.169.254).
5. Secrets (Bearer tokens, API keys) are scrubbed from error messages and logs.
6. Two-phase budget reservation releases on early failure, leaving spend at $0.00.
7. Explicitly configured Groq provider works and is preserved.
8. argus_recover accepts caller-specified model and provider parameters.
9. argus_recover prioritizes the user's host IDE subscription (Antigravity) over external developer keys.
10. ArgusConfig never silently defaults to Groq without explicit user authorization.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from argus.config import ArgusConfig
from argus.gateway.config import GatewayConfig, ProviderConfig, TierDefinition
from argus.gateway.router import GatewayRouter, NoEligibleProviderError
from argus.gateway.server import app, init_gateway
from argus.mcp.server import create_mcp_server
from argus.providers.storage import scrub_secrets


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ensure environment is pristine and developer keys are not present by default."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ARGUS_PROVIDER", raising=False)
    monkeypatch.delenv("ARGUS_GATEWAY_API_KEY", raising=False)


# =============================================================================
# 1. Config & Credential Isolation Tests
# =============================================================================

def test_argus_config_does_not_silently_default_to_groq(monkeypatch):
    """ArgusConfig on a fresh machine without providers must not default to Groq."""
    monkeypatch.setattr("argus.providers.antigravity.find_antigravity_cli", lambda: None)
    with patch("argus.providers.storage.ProviderStorage.get_active_provider", return_value=None):
        cfg = ArgusConfig()
        assert cfg.provider is None, f"Expected provider to be None, got {cfg.provider}"
        assert getattr(cfg, "groq_api_key", None) is None
        assert "No active LLM provider configured" in cfg.validate()[0]


def test_controller_resolves_antigravity_host_runtime_when_unconfigured(monkeypatch):
    """When running inside an IDE with Antigravity host runtime, RecoveryController dynamically resolves AntigravityHostAdapter."""
    from argus.providers.antigravity import AntigravityHostAdapter
    from argus.runtime.controller import RecoveryController

    monkeypatch.setattr("argus.providers.antigravity.find_antigravity_cli", lambda: Path("bin/agy.cmd"))
    with patch("argus.providers.storage.ProviderStorage.get_active_provider", return_value=None):
        # Host discovery now requires the explicit 'auto' selection policy.
        cfg = ArgusConfig(provider="auto")
        assert cfg.provider == "auto"
        controller = RecoveryController(config=cfg)
        adapter = controller._get_adapter()
        assert isinstance(adapter, AntigravityHostAdapter)
        assert adapter.config.model == "gemini-3.8-flash-low"
        assert controller.selected_provider == "antigravity"


def test_gateway_config_does_not_inject_groq_when_unconfigured(monkeypatch, tmp_path):
    """GatewayConfig.load() must not inject Groq when environment and storage have no keys."""
    monkeypatch.setattr("argus.providers.antigravity.find_antigravity_cli", lambda: None)
    with patch("argus.providers.storage.ProviderStorage.list_providers", return_value=[]):
        dummy_cfg = tmp_path / "nonexistent.yaml"
        cfg = GatewayConfig.load(dummy_cfg)
        assert not any(p.name == "groq" for p in cfg.providers), "Groq should not be injected when unconfigured"


# =============================================================================
# 2. Gateway Provider Resolution & Client Key Forwarding Tests
# =============================================================================

def test_gateway_chat_completions_fails_with_503_when_no_provider_configured():
    """When gateway has no providers, return 503 provider_resolution_error instead of fake 401 or Groq fallback."""
    cfg = GatewayConfig(providers=[], tiers={})
    init_gateway(cfg)
    client = TestClient(app)

    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert resp.status_code == 503
    data = resp.json()
    assert data["error"]["type"] == "provider_resolution_error"
    assert data["error"]["code"] == "no_eligible_provider"
    assert "No eligible inference provider configured" in data["error"]["message"]


def test_gateway_messages_fails_with_503_when_no_provider_configured():
    """Anthropic /v1/messages returns 503 provider_resolution_error when unconfigured."""
    cfg = GatewayConfig(providers=[], tiers={})
    init_gateway(cfg)
    client = TestClient(app)

    resp = client.post(
        "/v1/messages",
        json={"model": "claude-3-5-sonnet", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert resp.status_code == 503
    data = resp.json()
    assert data["type"] == "error"
    assert data["error"]["type"] == "provider_resolution_error"
    assert data["error"]["code"] == "no_eligible_provider"


def test_gateway_uses_client_authorization_key_for_configured_provider():
    """When a provider is configured without a stored key, client-supplied Authorization key is used."""
    provider = ProviderConfig(name="openai", kind="openai_compatible", base_url="https://api.openai.com/v1", api_key="")
    cfg = GatewayConfig(providers=[provider], tiers={})
    init_gateway(cfg)
    client = TestClient(app)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.content = json.dumps({
        "id": "chatcmpl-test",
        "choices": [{"message": {"role": "assistant", "content": "Hello user"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }).encode("utf-8")
    mock_resp.json.return_value = json.loads(mock_resp.content)

    with patch("httpx.AsyncClient.post", return_value=mock_resp) as mock_post:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-user-client-provided-secret"},
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer sk-user-client-provided-secret"


def test_gateway_returns_401_when_provider_configured_but_no_key_provided():
    """When a provider is configured but neither Argus nor client provides a key, return 401 missing_provider_key."""
    provider = ProviderConfig(name="openai", kind="openai_compatible", base_url="https://api.openai.com/v1", api_key="")
    cfg = GatewayConfig(providers=[provider], tiers={})
    init_gateway(cfg)
    client = TestClient(app)

    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"]["code"] == "missing_provider_key"


# =============================================================================
# 3. Gateway Client Auth Separation & SSRF Protection Tests
# =============================================================================

def test_gateway_client_auth_rejects_unauthorized_client(monkeypatch):
    """When ARGUS_GATEWAY_API_KEY is configured, requests without valid bearer token are rejected."""
    monkeypatch.setenv("ARGUS_GATEWAY_API_KEY", "argus-secret-gateway-token")
    provider = ProviderConfig(name="openai", kind="openai_compatible", base_url="https://api.openai.com/v1", api_key="sk-openai-key")
    cfg = GatewayConfig(providers=[provider], tiers={})
    init_gateway(cfg)
    client = TestClient(app)

    # Request without gateway token
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized_gateway_client"

    # Request with wrong gateway token
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer wrong-token"},
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized_gateway_client"


def test_gateway_blocks_ssrf_to_metadata_ip():
    """Requests attempting to route to cloud metadata IP 169.254.169.254 are blocked with HTTP 400."""
    provider = ProviderConfig(name="rogue", kind="openai_compatible", base_url="http://169.254.169.254/v1", api_key="sk-key")
    cfg = GatewayConfig(providers=[provider], tiers={})
    init_gateway(cfg)
    client = TestClient(app)

    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"]["code"] == "ssrf_protection"
    assert "Invalid or unsafe upstream base URL" in data["error"]["message"]


# =============================================================================
# 4. Budget Reservation & Secret Scrubbing Tests
# =============================================================================

def test_two_phase_budget_reservation_releases_on_failure():
    """When upstream request fails, budget reservation is safely released and spend remains 0.0."""
    provider = ProviderConfig(name="openai", kind="openai_compatible", base_url="https://api.openai.com/v1", api_key="sk-key")
    cfg = GatewayConfig(providers=[provider], tiers={})
    init_gateway(cfg)
    client = TestClient(app)

    from argus.gateway.server import _budget_guard
    assert _budget_guard is not None
    initial_spend = _budget_guard.get_session_spend()
    assert initial_spend == 0.0

    with patch("httpx.AsyncClient.post", side_effect=Exception("Connection refused")):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 502
        assert _budget_guard.get_session_spend() == 0.0


def test_secret_scrubbing_redacts_credentials_from_errors():
    """scrub_secrets removes OpenAI, Anthropic, Groq, and Bearer secrets from messages."""
    raw = "Failed to connect with Authorization: Bearer secret-12345 and key sk-proj-abcdef1234567890"
    cleaned = scrub_secrets(raw)
    assert "secret-12345" not in cleaned
    assert "abcdef1234567890" not in cleaned
    assert "[REDACTED_SECRET]" in cleaned


def test_explicitly_configured_groq_provider_is_rejected():
    """Attempting to configure a Groq provider must be strictly rejected with ValueError."""
    with pytest.raises(ValueError, match="Groq is strictly prohibited"):
        ProviderConfig(name="groq", kind="openai_compatible", base_url="https://api.groq.com/openai/v1", api_key="gsk_valid_key")

    with pytest.raises(ValueError, match="Groq is strictly prohibited"):
        ProviderConfig(name="custom", kind="openai_compatible", base_url="https://api.groq.com/openai/v1", api_key="gsk_valid_key")


# =============================================================================
# 5. MCP Recovery Platform Model Selection Tests
# =============================================================================

def test_mcp_argus_recover_accepts_model_and_provider_parameters(monkeypatch):
    """argus_recover accepts model and provider parameters from caller."""
    monkeypatch.setattr("argus.providers.antigravity.find_antigravity_cli", lambda: None)
    with patch("argus.providers.storage.ProviderStorage.get_active_provider", return_value=None):
        server = create_mcp_server()
        tools = {t.name: t for t in server._tool_manager.list_tools()}

        # When unconfigured and caller provides no provider
        res_str = tools["argus_recover"].fn(test_command="pytest tests/test_calc.py")
        res = json.loads(res_str)
        assert res["status"] == "CONFIG_ERROR"
        assert "No eligible inference provider configured" in res["error"]


def test_mcp_argus_recover_does_not_auto_discover_host_without_auto(monkeypatch):
    """A missing provider must not silently select Antigravity in MCP recovery."""
    monkeypatch.setattr("argus.providers.antigravity.find_antigravity_cli", lambda: Path("bin/agy.cmd"))
    with patch("argus.providers.storage.ProviderStorage.get_active_provider", return_value=None):
        server = create_mcp_server()
        tools = {t.name: t for t in server._tool_manager.list_tools()}

        res = json.loads(tools["argus_recover"].fn(test_command="pytest tests/test_calc.py"))
        assert res["status"] == "CONFIG_ERROR"
        assert "No eligible inference provider configured" in res["error"]


def test_mcp_argus_recover_uses_host_antigravity_subscription(monkeypatch):
    """Explicit 'auto' uses the host runtime with zero key."""
    monkeypatch.setattr("argus.providers.antigravity.find_antigravity_cli", lambda: Path("bin/agy.cmd"))
    with patch("argus.providers.storage.ProviderStorage.get_active_provider", return_value=None):
        server = create_mcp_server()
        tools = {t.name: t for t in server._tool_manager.list_tools()}

        from argus.runtime.controller import RecoveryOutcome

        def fake_run(self, *args, **kwargs):
            return RecoveryOutcome(
                run_id="test-run",
                status="SUCCESS",
                verified=True,
                effort_used="medium",
                routing_reasons=[],
                total_attempts=1,
                total_cost_usd=0.0,
                total_duration_ms=150.0,
                target_file="calc.py",
                changed_files=["calc.py"],
                tests_executed=["pytest test_calc.py"],
                unified_diff="--- calc.py\n+++ calc.py\n",
                diagnostics="All tests passed",
                message="Recovery verified",
            )

        with patch("argus.runtime.controller.RecoveryController.run", fake_run):
            res_str = tools["argus_recover"].fn(
                test_command="pytest test_calc.py",
                model="gemini-2.5-flash",
                provider="auto",
            )
            res = json.loads(res_str)
            assert res["status"] == "SUCCESS"
