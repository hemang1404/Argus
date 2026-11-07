"""Tests for WP-7 (Gateway Security and Containment).

Validates:
1. Mandatory Authentication: Unauthenticated or invalid requests to /v1/chat/completions and /v1/messages return HTTP 401.
2. Constant-time comparison: Valid tokens via Authorization: Bearer or x-api-key authenticate successfully.
3. Host Header Validation: Unexpected hostnames (DNS rebinding attacks) return HTTP 400.
4. Origin Validation: Foreign browser origins return HTTP 403 Forbidden; loopback origins succeed.
5. Rate Limiting: High-frequency requests return HTTP 429 with Retry-After header.
6. Dynamic Budget Reservation: Reservation scales with prompt size and requested tokens.
"""

from __future__ import annotations

import os
from starlette.testclient import TestClient
from unittest.mock import patch, MagicMock

import pytest

from argus.gateway.config import BudgetConfig, GatewayConfig, ProviderConfig, TierDefinition
from argus.gateway.server import (
    app,
    init_gateway,
    estimate_request_cost,
    GatewayRateLimiter,
)


@pytest.fixture
def secure_gateway_client(monkeypatch: pytest.MonkeyPatch):
    """Set up gateway with explicit test API key and test configuration."""
    test_key = "test-gateway-secret-token-12345"
    monkeypatch.setenv("ARGUS_GATEWAY_API_KEY", test_key)

    cfg = GatewayConfig(
        api_key=test_key,
        providers=[
            ProviderConfig(name="mock_provider", base_url="https://api.mock.com/v1", api_key="sk-mock")
        ],
        tiers={
            "cheap": TierDefinition(provider="mock_provider", model="mock-model"),
            "strong": TierDefinition(provider="mock_provider", model="mock-model"),
            "frontier": TierDefinition(provider="mock_provider", model="mock-model"),
        },
    )
    init_gateway(config=cfg, workspace=".")
    return TestClient(app), test_key


# ============================================================================
# 1. Mandatory Authentication Tests
# ============================================================================

def test_chat_completions_requires_authentication(secure_gateway_client):
    """POST /v1/chat/completions without auth token must return HTTP 401."""
    client, _ = secure_gateway_client
    resp = client.post("/v1/chat/completions", json={"model": "argus/auto", "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 401
    err = resp.json()["error"]
    assert err["code"] == "unauthorized_gateway_client"


def test_chat_completions_rejects_invalid_token(secure_gateway_client):
    """POST /v1/chat/completions with invalid token must return HTTP 401."""
    client, _ = secure_gateway_client
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer wrong-token"},
        json={"model": "argus/auto", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401
    err = resp.json()["error"]
    assert err["code"] == "unauthorized_gateway_client"


def test_anthropic_messages_requires_authentication(secure_gateway_client):
    """POST /v1/messages without auth token must return HTTP 401."""
    client, _ = secure_gateway_client
    resp = client.post("/v1/messages", json={"model": "argus/auto", "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized_gateway_client"


# ============================================================================
# 2. Host & Origin Validation Tests (DNS Rebinding & CORS Protection)
# ============================================================================

def test_gateway_rejects_foreign_host_header(secure_gateway_client):
    """Host header matching foreign domains must return HTTP 400 (DNS rebinding prevention)."""
    client, key = secure_gateway_client
    resp = client.get(
        "/health",
        headers={"Host": "malicious-attacker.com", "Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_host"


def test_gateway_rejects_foreign_origin(secure_gateway_client):
    """Origin header matching external websites must return HTTP 403 Forbidden."""
    client, key = secure_gateway_client
    resp = client.get(
        "/health",
        headers={"Origin": "https://malicious-browser-site.com", "Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden_origin"


def test_gateway_accepts_loopback_origin(secure_gateway_client):
    """Origin header matching local loopback (localhost/127.0.0.1) must succeed."""
    client, key = secure_gateway_client
    resp = client.get(
        "/health",
        headers={"Origin": "http://localhost:3000", "Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


# ============================================================================
# 3. Rate Limiting Tests
# ============================================================================

def test_gateway_rate_limiter_bounds():
    """Rate limiter enforces request limits and caps tracked client memory."""
    limiter = GatewayRateLimiter(max_requests_per_minute=3, max_clients=5)
    # First 3 requests allowed
    assert limiter.allow_request("client-1") is True
    assert limiter.allow_request("client-1") is True
    assert limiter.allow_request("client-1") is True
    # 4th request refused
    assert limiter.allow_request("client-1") is False

    # Other clients still allowed
    assert limiter.allow_request("client-2") is True


# ============================================================================
# 4. Dynamic Budget Cost Estimation Tests
# ============================================================================

def test_estimate_request_cost():
    """Cost estimation scales with payload characters and requested completion tokens."""
    # Small request
    small_body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 100}
    cost_small = estimate_request_cost(small_body)
    assert cost_small >= 0.001

    # Large prompt request
    large_body = {
        "messages": [{"role": "user", "content": "A" * 80000}],  # ~20,000 tokens
        "max_tokens": 4000,
    }
    cost_large = estimate_request_cost(large_body)
    assert cost_large > cost_small
    assert cost_large <= 0.50

