"""Tests for Argus Middleware Hardening, Security Invariants, and Host Integration.

Covers:
- Task 1: Credential Isolation & Provider Boundary Enforcement
- Task 2: Budget & Uncertain Billing Semantics
- Task 3: SSRF & Upstream Destination Validation
- Task 4: Real Antigravity Host Integration
- Task 5: Provider & Model Selection Enforcement
- Task 6: Gateway Compatibility (Streaming, Tools)
- Task 7: Accounting Semantics & Telemetry Distinctions
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from argus.config import ArgusConfig
from argus.gateway.budget import BudgetConfig, BudgetGuard
from argus.gateway.config import GatewayConfig, ProviderConfig, TierDefinition
from argus.gateway.retry import FailureClassification, classify_exception, is_retryable, is_uncertain_execution
from argus.gateway.router import GatewayRouter, NoEligibleProviderError
from argus.gateway.server import app, init_gateway, validate_upstream_url
from argus.gateway.telemetry import TelemetryRegistry
from argus.mcp.server import create_mcp_server
from argus.patch.ast_checker import ASTChecker
from argus.providers.adapter import ProviderError
from argus.providers.antigravity import AntigravityHostAdapter, find_antigravity_cli
from argus.providers.storage import scrub_secrets


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ensure environment is pristine for every test."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ARGUS_PROVIDER", raising=False)
    monkeypatch.delenv("ARGUS_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("AGY_SIMULATE_UNAUTHENTICATED", raising=False)


# =============================================================================
# Task 1: Credential Isolation & Cross-Provider Boundary Enforcement
# =============================================================================

def test_different_argus_and_upstream_credentials(monkeypatch):
    """Argus client token and upstream provider token remain strictly separated."""
    monkeypatch.setenv("ARGUS_GATEWAY_API_KEY", "gateway-client-secret")
    provider = ProviderConfig(name="openai", kind="openai_compatible", base_url="https://api.openai.com/v1", api_key="")
    cfg = GatewayConfig(providers=[provider], tiers={})
    init_gateway(cfg)
    client = TestClient(app)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.content = json.dumps({
        "id": "chatcmpl-test",
        "choices": [{"message": {"role": "assistant", "content": "Success"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }).encode("utf-8")
    mock_resp.json.return_value = json.loads(mock_resp.content)

    with patch("httpx.AsyncClient.post", return_value=mock_resp) as mock_post:
        resp = client.post(
            "/v1/chat/completions",
            headers={
                "X-Argus-Key": "gateway-client-secret",
                "X-Upstream-Api-Key": "sk-real-upstream-secret",
            },
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        _, kwargs = mock_post.call_args
        # Upstream MUST receive the upstream key, NEVER the gateway client key
        assert kwargs["headers"]["Authorization"] == "Bearer sk-real-upstream-secret"


def test_client_bearer_token_not_forwarded_when_argus_gateway_key_configured(monkeypatch):
    """When ARGUS_GATEWAY_API_KEY is configured, Bearer token is consumed as gateway auth and not forwarded."""
    monkeypatch.setenv("ARGUS_GATEWAY_API_KEY", "my-gateway-token")
    provider = ProviderConfig(name="openai", kind="openai_compatible", base_url="https://api.openai.com/v1", api_key="")
    cfg = GatewayConfig(providers=[provider], tiers={})
    init_gateway(cfg)
    client = TestClient(app)

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer my-gateway-token"},
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    # Gateway auth succeeds, but provider has no key -> returns 401 missing_provider_key
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_provider_key"


def test_cross_provider_credential_violation_rejected():
    """Client cannot forward a key bound to provider A when the request routes to provider B."""
    p_openai = ProviderConfig(name="openai", kind="openai_compatible", base_url="https://api.openai.com/v1", api_key="")
    cfg = GatewayConfig(providers=[p_openai], tiers={})
    init_gateway(cfg)
    client = TestClient(app)

    resp = client.post(
        "/v1/chat/completions",
        headers={
            "X-Upstream-Api-Key": "sk-ant-exclusive-secret",
            "X-Upstream-Provider": "anthropic",
        },
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"]["code"] == "credential_provider_mismatch"
    assert "cross-provider credential forwarding" in data["error"]["message"].lower()


def test_client_supplied_key_overrides_configured_key():
    """Explicit per-request client-supplied upstream key overrides default configured provider key."""
    provider = ProviderConfig(name="openai", kind="openai_compatible", base_url="https://api.openai.com/v1", api_key="sk-configured-default")
    cfg = GatewayConfig(providers=[provider], tiers={})
    init_gateway(cfg)
    client = TestClient(app)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.content = json.dumps({
        "id": "chatcmpl-test",
        "choices": [{"message": {"role": "assistant", "content": "Success"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }).encode("utf-8")
    mock_resp.json.return_value = json.loads(mock_resp.content)

    with patch("httpx.AsyncClient.post", return_value=mock_resp) as mock_post:
        resp = client.post(
            "/v1/chat/completions",
            headers={"X-Upstream-Api-Key": "sk-client-explicit-override"},
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer sk-client-explicit-override"


# =============================================================================
# Task 2: Budget & Uncertain Billing Correctness
# =============================================================================

def test_read_timeout_records_uncertain_spend_and_does_not_release_to_zero(tmp_path):
    """Read timeouts represent post-dispatch uncertain execution and must record uncertain liability."""
    TelemetryRegistry._instance = None
    telemetry = TelemetryRegistry.get(workspace_root=tmp_path)

    provider = ProviderConfig(name="openai", kind="openai_compatible", base_url="https://api.openai.com/v1", api_key="sk-key")
    cfg = GatewayConfig(providers=[provider], tiers={})
    init_gateway(cfg, workspace=tmp_path)
    client = TestClient(app)

    from argus.gateway.server import _budget_guard
    assert _budget_guard is not None

    with patch("httpx.AsyncClient.post", side_effect=httpx.ReadTimeout("Connection dropped after dispatch")):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 504
        assert resp.json()["error"]["code"] == "upstream_timeout"

        # Liability must NOT be reported as $0.00
        assert _budget_guard.get_uncertain_spend() > 0.0
        summary = telemetry.get_summary()
        assert summary.uncertain_spend_usd > 0.0


def test_connect_timeout_releases_reservation_as_zero_spend(tmp_path):
    """Pre-dispatch connect timeouts fail before reaching provider, releasing reservation to 0.0."""
    provider = ProviderConfig(name="openai", kind="openai_compatible", base_url="https://api.openai.com/v1", api_key="sk-key")
    cfg = GatewayConfig(providers=[provider], tiers={})
    init_gateway(cfg, workspace=tmp_path)
    client = TestClient(app)

    from argus.gateway.server import _budget_guard
    assert _budget_guard is not None

    with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectTimeout("DNS resolution or TCP failed")):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 502
        assert _budget_guard.get_session_spend() == 0.0
        assert _budget_guard.get_uncertain_spend() == 0.0


def test_concurrent_budget_reservations_thread_safety():
    """Concurrent threads reserving budget cannot exceed configured session limit due to race conditions."""
    bg = BudgetGuard(BudgetConfig(per_session_usd=0.05, on_exhausted="fail_fast"))
    successes = []
    failures = []

    def worker(i: int):
        ok, err, res_id = bg.reserve(f"run-{i}", estimated_amount=0.01)
        if ok:
            successes.append(res_id)
        else:
            failures.append(err)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly 5 reservations of 0.01 can succeed for a 0.05 budget
    assert len(successes) == 5
    assert len(failures) == 5


# =============================================================================
# Task 3: SSRF and Upstream Destination Validation
# =============================================================================

@pytest.mark.parametrize(
    "unsafe_url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://0xa9fea9fe/v1",                    # Hex for 169.254.169.254
        "http://2852039166/v1",                    # Dword for 169.254.169.254
        "http://0251.0376.0251.0376/v1",           # Octal for 169.254.169.254
        "http://[::ffff:169.254.169.254]/v1",      # IPv6-mapped IPv4
        "http://[fe80::1]/v1",                     # IPv6 link-local
        "http://100.100.100.200/latest/meta-data/", # Alibaba Cloud metadata
        "http://metadata.google.internal/computeMetadata/v1", # GCP metadata
        "http://instance-data/latest/meta-data/",  # OpenStack/AWS instance-data
        "file:///etc/passwd",                      # Invalid scheme
        "gopher://127.0.0.1:8000/",                # Invalid scheme
        "ftp://api.openai.com/v1",                 # Invalid scheme
        "http://admin:secret@api.openai.com/v1",   # Userinfo in upstream URL
        "http://api.openai.com:70000/v1",          # Out-of-bounds port
        "https://api.groq.com/openai/v1",          # Prohibited Groq endpoint
        "https://groq.com/v1",                     # Prohibited Groq domain
    ],
)
def test_ssrf_blocks_unsafe_destinations(unsafe_url):
    """validate_upstream_url must reject metadata, alternate IP encodings, invalid schemes, userinfo, and prohibited Groq."""
    assert not validate_upstream_url(unsafe_url), f"Expected URL to be blocked: {unsafe_url}"


@pytest.mark.parametrize(
    "safe_url",
    [
        "https://api.openai.com/v1",
        "https://api.anthropic.com/v1",
        "https://api.deepseek.com/v1",
        "http://127.0.0.1:11434/v1",               # Local Ollama
        "http://localhost:8000/v1",                # Local vLLM
        "host://antigravity-cli",                  # Supported host scheme
    ],
)
def test_ssrf_permits_authorized_destinations(safe_url):
    """validate_upstream_url allows valid external and local providers."""
    assert validate_upstream_url(safe_url), f"Expected URL to be permitted: {safe_url}"


# =============================================================================
# Task 4: Real Antigravity Host Integration Tests
# =============================================================================

def test_antigravity_repo_fixture_execution():
    """Verify Antigravity repository test fixture (bin/agy.cmd or bin/agy.py) capability and synthesis."""
    repo_cli = Path.cwd() / "bin" / ("agy.cmd" if os.name == "nt" else "agy")
    if not repo_cli.is_file():
        pytest.skip("Repository bin/agy test fixture not present")

    adapter = AntigravityHostAdapter(cli_path=repo_cli)
    capabilities = adapter.discover_capabilities()
    assert capabilities["supports_effort"] is True
    assert len(capabilities["models"]) > 0

    conn = adapter.test_connection()
    assert conn.success is True
    assert conn.status_code == 200

    # Run completion with synthesize_repair
    resp = adapter.chat_completion(
        messages=[{"role": "user", "content": "Fix def multiply(a, b): return a + b"}],
        effort="medium",
    )
    assert resp.content is not None
    assert "return a * b" in resp.content
    assert resp.raw_response["effort"] == "medium"

    # Verify synthesized patch block contains valid replacement code
    if "=======" in resp.content and ">>>>>>> REPLACE" in resp.content:
        replacement_code = resp.content.split("=======")[1].split(">>>>>>> REPLACE")[0].strip()
    else:
        replacement_code = resp.content
    ast_res = ASTChecker.validate_python_code(replacement_code)
    assert ast_res.is_valid is True


def test_antigravity_cli_simulated_authentication_failure(monkeypatch):
    """When the test fixture returns unauthenticated error, adapter raises clear ProviderError 401."""
    monkeypatch.setenv("AGY_SIMULATE_UNAUTHENTICATED", "1")
    repo_cli = Path.cwd() / "bin" / ("agy.cmd" if os.name == "nt" else "agy")
    if not repo_cli.is_file():
        pytest.skip("Repository bin/agy test fixture not present")
    adapter = AntigravityHostAdapter(cli_path=repo_cli)
    conn = adapter.test_connection()
    assert conn.success is False
    assert conn.status_code == 401
    assert "unauthenticated" in conn.message.lower()


def test_real_antigravity_host_runtime_discovery_and_connection():
    """Verify that Argus detects and probes the actual installed Antigravity host runtime (agentapi.bat)."""
    host_cli = Path.home() / ".gemini" / "antigravity-ide" / "bin" / "agentapi.bat"
    if not host_cli.is_file():
        pytest.skip("Antigravity IDE host runtime agentapi.bat not present on this machine")

    # Verify find_antigravity_cli discovers installed host CLI (agy prioritized, agentapi supported)
    found = find_antigravity_cli()
    assert found is not None
    assert "agy" in found.name.lower() or "agentapi" in found.name.lower()

    adapter = AntigravityHostAdapter(cli_path=host_cli)
    capabilities = adapter.discover_capabilities()
    assert capabilities["is_agentapi"] is True
    assert capabilities["supports_effort"] is True

    conn = adapter.test_connection()
    if not conn.success:
        pytest.skip(f"Antigravity IDE runtime agentapi.bat cannot execute: {conn.message}")
    assert conn.success is True
    assert conn.status_code == 200
    assert "Usage: agentapi" in conn.response_sample or "Available Commands" in conn.response_sample


def test_antigravity_missing_cli_raises_404():
    """Attempting to invoke an adapter with nonexistent CLI path raises 404 ProviderError."""
    adapter = AntigravityHostAdapter(cli_path=Path("nonexistent/agy.exe"))
    with pytest.raises(ProviderError) as exc_info:
        adapter.chat_completion([{"role": "user", "content": "ping"}])
    assert exc_info.value.status_code == 404


def test_antigravity_timeout_handling():
    """Adapter enforces execution timeouts and returns 504 on timeout."""
    if find_antigravity_cli() is None:
        pytest.skip("Antigravity CLI not installed on host for timeout test")
    adapter = AntigravityHostAdapter(timeout_seconds=0.0001)
    with pytest.raises(ProviderError) as exc_info:
        adapter.chat_completion([{"role": "user", "content": "ping"}])
    assert exc_info.value.status_code == 504


# =============================================================================
# Task 5: Provider & Model Selection Enforcement
# =============================================================================

def test_unconfigured_provider_prefix_rejected_by_router():
    """Requesting 'mistral/mistral-large' when Mistral is not configured raises NoEligibleProviderError."""
    p_openai = ProviderConfig(name="openai", kind="openai_compatible", base_url="https://api.openai.com/v1", api_key="sk-key")
    cfg = GatewayConfig(providers=[p_openai], tiers={})
    router = GatewayRouter(cfg)

    with pytest.raises(NoEligibleProviderError) as exc_info:
        router.resolve(requested_model="mistral/mistral-large")
    assert "Requested provider 'mistral' is not configured" in str(exc_info.value)


def test_mcp_argus_recover_rejects_unconfigured_explicit_provider():
    """Explicitly passing an unconfigured provider to argus_recover returns CONFIG_ERROR, never silent Antigravity."""
    server = create_mcp_server()
    tools = {t.name: t for t in server._tool_manager.list_tools()}

    res_str = tools["argus_recover"].fn(
        test_command="pytest tests/test_calc.py",
        provider="unconfigured_provider_xyz",
    )
    res = json.loads(res_str)
    assert res["status"] == "CONFIG_ERROR"
    assert "Requested provider 'unconfigured_provider_xyz' is not configured" in res["error"]


# =============================================================================
# Task 6 & 7: Gateway Compatibility & Accounting Semantics
# =============================================================================

def test_gateway_streaming_chat_completions():
    """Gateway handles streaming chat completions, emitting SSE chunks and [DONE]."""
    provider = ProviderConfig(name="openai", kind="openai_compatible", base_url="https://api.openai.com/v1", api_key="sk-key")
    cfg = GatewayConfig(providers=[provider], tiers={})
    init_gateway(cfg)
    client = TestClient(app)

    async def mock_aiter_lines():
        yield 'data: {"choices":[{"delta":{"content":"Hello world"}}]}'
        yield 'data: [DONE]'

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.aiter_lines = mock_aiter_lines
    mock_resp.__aenter__.return_value = mock_resp

    with patch("httpx.AsyncClient.send", return_value=mock_resp):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        assert "Hello world" in resp.text
        assert "data: [DONE]" in resp.text


def test_telemetry_accounting_semantics_distinctions(tmp_path):
    """Telemetry strictly distinguishes provider-reported vs locally-estimated and measured vs uncertain spend."""
    TelemetryRegistry._instance = None
    reg = TelemetryRegistry.get(workspace_root=tmp_path)

    # 1. Confirmed turn
    e1 = reg.record_turn(
        client="aider",
        requested_model="gpt-4o",
        routed_model="gpt-4o-mini",
        tier="cheap",
        prompt_tokens=100,
        completion_tokens=50,
        latency_ms=120.0,
        cost_usd=0.0001,
        usage_source="provider_reported",
        cost_status="estimated",
    )
    assert e1.usage_source == "provider_reported"
    assert e1.cost_status == "estimated"
    assert e1.uncertain_usd == 0.0

    # 2. Uncertain turn
    e2 = reg.record_turn(
        client="aider",
        requested_model="gpt-4o",
        routed_model="gpt-4o-mini",
        tier="cheap",
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=120000.0,
        cost_usd=0.0,
        usage_source="unknown",
        cost_status="uncertain",
        uncertain_usd=0.001,
    )
    assert e2.cost_status == "uncertain"
    assert e2.uncertain_usd == 0.001

    summary = reg.get_summary()
    assert summary.session_spend_usd == 0.0001
    assert summary.uncertain_spend_usd == 0.001


def test_conflicting_client_credentials_rejected():
    """When client supplies conflicting keys in Authorization and X-Upstream-Api-Key without gateway token, reject with 400."""
    client = TestClient(app)
    headers = {
        "Authorization": "Bearer sk-key-alpha",
        "X-Upstream-Api-Key": "sk-key-beta",
    }
    resp = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"]["code"] == "conflicting_credentials"


def test_client_key_with_unmatched_signature_rejected_without_header():
    """Client supplies Anthropic key (sk-ant-...) to model routing to OpenAI without X-Upstream-Provider; rejected with 400."""
    client = TestClient(app)
    headers = {
        "Authorization": "Bearer sk-ant-mismatched_secret_12345",
    }
    resp = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"]["code"] == "credential_provider_mismatch"
    assert "cross-provider" in data["error"]["message"].lower()


def test_groq_key_signature_strictly_prohibited():
    """Client supplies Groq key signature (gsk_...); strictly rejected with prohibited_provider error."""
    client = TestClient(app)
    headers = {
        "Authorization": "Bearer gsk_groq_prohibited_secret_12345",
    }
    resp = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"]["code"] == "prohibited_provider"
    assert "Groq" in data["error"]["message"]


def test_client_key_missing_provider_binding_rejected():
    """Client supplies opaque key with no signature and no X-Upstream-Provider; rejected with 400."""
    client = TestClient(app)
    headers = {
        "X-Upstream-Api-Key": "opaque_custom_token_no_signature",
    }
    resp = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"]["code"] == "missing_provider_binding"


def test_ssrf_dns_rebinding_blocked(monkeypatch):
    """Connection-time DNS resolution blocking public domains resolving to loopback or private ranges."""
    import socket

    orig_getaddrinfo = socket.getaddrinfo

    def mock_getaddrinfo(host, port, *args, **kwargs):
        if host == "api.evil.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        elif host == "api.safe-public.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        return orig_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)
    assert not validate_upstream_url("http://api.evil.com/v1", resolve_dns=True)
    assert validate_upstream_url("http://api.safe-public.com/v1", resolve_dns=True)


def test_budget_failure_taxonomy_matrix():
    """Verify atomic budget accounting across failure states: pre-dispatch refused vs read-timeout uncertain vs 500 error."""
    guard = BudgetGuard(BudgetConfig(per_session_usd=0.010, per_task_usd=0.005))

    # 1. Pre-dispatch connection failure (e.g. ConnectionRefused / ConnectTimeout)
    ok, _, res1 = guard.reserve("task-1", estimated_cost=0.002)
    assert ok is True
    assert guard._reserved_session_usd == 0.002
    # Connection fails before upstream dispatch -> released to 0.0
    guard.release(res1)
    assert guard.get_session_spend() == 0.0
    assert guard._reserved_session_usd == 0.0
    assert guard.get_uncertain_spend() == 0.0

    # 2. Post-dispatch read timeout -> liability cannot be assumed free
    ok, _, res2 = guard.reserve("task-1", estimated_cost=0.002)
    assert ok is True
    guard.record_uncertain(res2, "task-1", "gpt-4o", 0.002)
    assert guard.get_session_spend() == 0.0
    assert guard._reserved_session_usd == 0.0
    assert guard.get_uncertain_spend() == 0.002

    # 3. Upstream HTTP 500 error without token usage -> liability cannot be assumed free ($0.00)
    ok, _, res3 = guard.reserve("task-1", estimated_cost=0.002)
    assert ok is True
    guard.record_uncertain(res3, "task-1", "gpt-4o", 0.002)
    assert guard.get_uncertain_spend() == 0.004

    # 4. Atomic check near limit: remaining available is 0.010 - 0.004 (uncertain) = 0.006
    # A burst requesting 0.007 must be rejected!
    ok_burst, msg_burst, res_burst = guard.reserve("task-2", estimated_cost=0.007)
    assert ok_burst is False
    assert res_burst is None
    assert "Session budget limit" in msg_burst


# =============================================================================
# Additional Verification & Hardening Regression Tests
# =============================================================================

def test_opaque_credential_missing_binding_rejected_transport_never_called(tmp_path):
    """Client supplies genuine opaque key with no X-Upstream-Provider; transport must NEVER be called."""
    provider = ProviderConfig(name="openai", kind="openai_compatible", base_url="https://api.openai.com/v1", api_key="sk-configured")
    cfg = GatewayConfig(providers=[provider], tiers={})
    init_gateway(cfg, workspace=tmp_path)
    client = TestClient(app)

    with patch("httpx.AsyncClient.post") as mock_post:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer opaque_custom_secret_key_12345"},
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "missing_provider_binding"
        mock_post.assert_not_called()


def test_opaque_credential_custom_openai_provider_routing(tmp_path):
    """Client supplies opaque key with explicit X-Upstream-Provider; forwarded when matched, rejected when mismatched."""
    p_custom = ProviderConfig(name="custom-openai", kind="openai_compatible", base_url="https://api.custom.internal/v1", api_key="")
    cfg = GatewayConfig(providers=[p_custom], tiers={"cheap": TierDefinition(provider="custom-openai", model="custom-model")})
    init_gateway(cfg, workspace=tmp_path)
    client = TestClient(app)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.content = json.dumps({
        "id": "cmpl-custom",
        "choices": [{"message": {"role": "assistant", "content": "Custom model response"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5},
    }).encode("utf-8")
    mock_resp.json.return_value = json.loads(mock_resp.content)

    # 1. Matched provider binding -> forwarded
    with patch("httpx.AsyncClient.post", return_value=mock_resp) as mock_post:
        resp = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer opaque-custom-key-xyz",
                "X-Upstream-Provider": "custom-openai",
            },
            json={"model": "argus/cheap", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer opaque-custom-key-xyz"

    # 2. Mismatched provider binding -> rejected with 400 and transport NEVER called
    with patch("httpx.AsyncClient.post") as mock_post:
        resp = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer opaque-custom-key-xyz",
                "X-Upstream-Provider": "anthropic",
            },
            json={"model": "argus/cheap", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "credential_provider_mismatch"
        mock_post.assert_not_called()


def test_conflicting_provider_header_and_signature_transport_never_called(tmp_path):
    """Header specifies anthropic but key signature is openai (sk-proj-...); transport must NEVER be called."""
    provider = ProviderConfig(name="anthropic", kind="anthropic", base_url="https://api.anthropic.com/v1", api_key="")
    cfg = GatewayConfig(providers=[provider], tiers={})
    init_gateway(cfg, workspace=tmp_path)
    client = TestClient(app)

    with patch("httpx.AsyncClient.post") as mock_post:
        resp = client.post(
            "/v1/chat/completions",
            headers={
                "X-Upstream-Provider": "anthropic",
                "Authorization": "Bearer sk-proj-conflicting_openai_key_99999",
            },
            json={"model": "claude-3-5-sonnet", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "conflicting_provider_binding"
        mock_post.assert_not_called()


def test_conflicting_credential_headers_transport_never_called(tmp_path):
    """Client supplies different keys in X-Upstream-Api-Key and X-Api-Key; transport must NEVER be called."""
    provider = ProviderConfig(name="openai", kind="openai_compatible", base_url="https://api.openai.com/v1", api_key="")
    cfg = GatewayConfig(providers=[provider], tiers={})
    init_gateway(cfg, workspace=tmp_path)
    client = TestClient(app)

    with patch("httpx.AsyncClient.post") as mock_post:
        resp = client.post(
            "/v1/chat/completions",
            headers={
                "X-Upstream-Api-Key": "sk-key-one",
                "X-Api-Key": "sk-key-two",
            },
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "conflicting_credentials"
        mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_dns_rebinding_socket_intercept_deterministic():
    """Verify SafeAnyIOBackend deterministically blocks connection when hostname resolves to restricted IP."""
    from argus.gateway.transport import SafeAsyncHTTPTransport
    import anyio

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1")
    port = listener.extra(anyio.abc.SocketAttribute.local_address)[1]

    orig_connect = anyio.connect_tcp
    async def mock_connect(remote_host, remote_port, **kwargs):
        if remote_host == "rebind.attacker.com":
            return await orig_connect("127.0.0.1", remote_port, **kwargs)
        return await orig_connect(remote_host, remote_port, **kwargs)

    anyio.connect_tcp = mock_connect

    try:
        transport = SafeAsyncHTTPTransport(allowed_hosts={"api.approved.local"})
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(httpx.ConnectError) as exc_info:
                await client.get(f"http://rebind.attacker.com:{port}/steal")
            assert "SSRF blocked at connection time" in str(exc_info.value)
    finally:
        anyio.connect_tcp = orig_connect
        await listener.aclose()


def test_dns_rebinding_mixed_records_preflight(monkeypatch):
    """validate_upstream_url must reject when DNS returns mixed public and private IPs."""
    import socket

    orig_getaddrinfo = socket.getaddrinfo

    def mixed_getaddrinfo(host, port, *args, **kwargs):
        if host == "mixed.attacker.com":
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port)),
            ]
        return orig_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", mixed_getaddrinfo)
    assert not validate_upstream_url("http://mixed.attacker.com/v1", resolve_dns=True)


def test_http_500_response_without_usage_preserves_uncertainty(tmp_path):
    """Upstream 500 without usage tokens cannot prove zero spend; preserves uncertainty."""
    provider = ProviderConfig(name="openai", kind="openai_compatible", base_url="https://api.openai.com/v1", api_key="sk-test")
    cfg = GatewayConfig(providers=[provider], tiers={})
    init_gateway(cfg, workspace=tmp_path)
    client = TestClient(app)

    from argus.gateway.server import _budget_guard
    assert _budget_guard is not None

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.text = json.dumps({"error": {"message": "Internal engine crash"}})
    mock_resp.content = mock_resp.text.encode("utf-8")
    mock_resp.json.return_value = json.loads(mock_resp.text)

    with patch("httpx.AsyncClient.post", return_value=mock_resp):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 500
        assert _budget_guard.get_session_spend() == 0.0
        assert _budget_guard.get_uncertain_spend() == 0.001


def test_http_500_response_with_reported_usage_commits_spend(tmp_path):
    """Upstream 500 that includes reported usage tokens commits the actual observed spend."""
    provider = ProviderConfig(name="openai", kind="openai_compatible", base_url="https://api.openai.com/v1", api_key="sk-test")
    cfg = GatewayConfig(providers=[provider], tiers={})
    init_gateway(cfg, workspace=tmp_path)
    client = TestClient(app)

    from argus.gateway.server import _budget_guard
    assert _budget_guard is not None

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.text = json.dumps({
        "error": {"message": "Context window exceeded"},
        "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
    })
    mock_resp.content = mock_resp.text.encode("utf-8")
    mock_resp.json.return_value = json.loads(mock_resp.text)

    with patch("httpx.AsyncClient.post", return_value=mock_resp):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 500
        assert _budget_guard.get_session_spend() > 0.0


def test_http_4xx_response_releases_budget_reservation(tmp_path):
    """Upstream 4xx indicates request rejection before model execution; cleanly releases reservation."""
    provider = ProviderConfig(name="openai", kind="openai_compatible", base_url="https://api.openai.com/v1", api_key="sk-test")
    cfg = GatewayConfig(providers=[provider], tiers={})
    init_gateway(cfg, workspace=tmp_path)
    client = TestClient(app)

    from argus.gateway.server import _budget_guard
    assert _budget_guard is not None

    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.text = json.dumps({"error": {"message": "Bad request"}})
    mock_resp.content = mock_resp.text.encode("utf-8")
    mock_resp.json.return_value = json.loads(mock_resp.text)

    with patch("httpx.AsyncClient.post", return_value=mock_resp):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 400
        assert _budget_guard.get_session_spend() == 0.0
        assert _budget_guard.get_uncertain_spend() == 0.0


def test_budget_duplicate_reconciliation_is_idempotent():
    """Duplicate calls to release, record_uncertain, or commit cannot double-count spend or double-release."""
    guard = BudgetGuard(BudgetConfig(per_session_usd=1.0))
    ok, _, res_id = guard.reserve("run-dup", estimated_cost=0.01)
    assert ok is True
    assert guard._reserved_session_usd == 0.01

    # First commit reconciles reservation and charges actual token cost
    cost1 = guard.commit(res_id, "run-dup", "gpt-4o", prompt_tokens=1000, completion_tokens=100)
    assert cost1 > 0.0
    initial_spend = guard.get_session_spend()
    assert initial_spend > 0.0
    assert guard._reserved_session_usd == 0.0

    # Second commit with same res_id must be an idempotent no-op!
    cost2 = guard.commit(res_id, "run-dup", "gpt-4o", prompt_tokens=1000, completion_tokens=100)
    assert cost2 == 0.0
    assert guard.get_session_spend() == initial_spend

    # Subsequent release or record_uncertain with same res_id must also be no-ops
    guard.release(res_id)
    assert guard.get_session_spend() == initial_spend
    unc = guard.record_uncertain(res_id, "run-dup", "gpt-4o", 0.05)
    assert unc == 0.0
    assert guard.get_uncertain_spend() == 0.0


def test_recovery_safety_fuzzy_engine_rejects_ambiguous_anchors():
    """ResilientPatchEngine refuses ambiguous patch blocks matching multiple locations."""
    from argus.patch.fuzzy_engine import ResilientPatchEngine, RefusalReason
    from argus.patch.parser import PatchBlock

    engine = ResilientPatchEngine()
    source_code = (
        "def calculate(x):\n"
        "    return x + 1\n\n"
        "def calculate_another(x):\n"
        "    return x + 1\n"
    )
    # Search block matches both functions identically
    patch_block = PatchBlock(
        search_text="    return x + 1\n",
        replace_text="    return x + 2\n",
    )
    result = engine.apply_patch(source_code, patch_block)
    assert not result.success
    assert result.refusal_reason == RefusalReason.REFUSED_AMBIGUOUS_MULTIPLE_EXACT_MATCHES


def test_recovery_safety_candidate_applier_rejections_preserve_target(tmp_path):
    """AuthoritativePatchApplier strictly refuses invalid patches and preserves original file on disk."""
    from argus.runtime.candidate import AuthoritativePatchApplier, CandidateArtifactStore
    import hashlib

    target = tmp_path / "calc.py"
    original_code = "def add(a, b):\n    return a + b\n"
    target.write_text(original_code, encoding="utf-8")
    base_hash = hashlib.sha256(original_code.encode("utf-8")).hexdigest()

    # 1. Invalid syntax in candidate -> refused with SYNTAX_VALIDATION_FAILED
    run_dir = CandidateArtifactStore.save_run_artifact(
        run_id="run-bad-syntax",
        target_file="calc.py",
        test_command="pytest",
        baseline_hash=base_hash,
        status="SUCCESS",
        verified=True,
        tier_used=1,
        attempts=1,
        cost_usd=0.001,
        duration_ms=100.0,
        patched_content="def add(a, b\n    return a + b\n",  # Syntax error
        unified_diff="--- a\n+++ b\n",
        workspace_root=tmp_path,
    )
    res = AuthoritativePatchApplier.apply("run-bad-syntax", workspace_root=tmp_path)
    assert not res.success
    assert res.error_code == "SYNTAX_VALIDATION_FAILED"
    assert target.read_text(encoding="utf-8") == original_code

    # 2. Stale target modification -> refused with STALE_TARGET_CONFLICT
    target.write_text("def add(a, b):\n    # modified concurrently\n    return a + b\n", encoding="utf-8")
    run_dir2 = CandidateArtifactStore.save_run_artifact(
        run_id="run-stale",
        target_file="calc.py",
        test_command="pytest",
        baseline_hash=base_hash,  # Matches original, but disk has changed!
        status="SUCCESS",
        verified=True,
        tier_used=1,
        attempts=1,
        cost_usd=0.001,
        duration_ms=100.0,
        patched_content="def add(a, b):\n    return a + b + 0\n",
        unified_diff="--- a\n+++ b\n",
        workspace_root=tmp_path,
    )
    res2 = AuthoritativePatchApplier.apply("run-stale", workspace_root=tmp_path)
    assert not res2.success
    assert res2.error_code == "STALE_TARGET_CONFLICT"


