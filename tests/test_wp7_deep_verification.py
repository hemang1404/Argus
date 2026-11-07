"""WP-7 Deep Verification: Route Authentication Boundary, Multi-Client Rate Limiting, and Budget Concurrency."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import threading
from typing import Dict, Any
from unittest.mock import patch, MagicMock

import pytest
from starlette.testclient import TestClient

from argus.config import MODEL_REGISTRY, ModelCost
from argus.gateway.budget import BudgetGuard
from argus.gateway.config import BudgetConfig, GatewayConfig, ProviderConfig, TierDefinition
from argus.gateway.server import (
    app,
    init_gateway,
    estimate_request_cost,
    GatewayRateLimiter,
    extract_rate_limit_identity,
    lifespan,
)


@pytest.fixture
def deep_gateway_client(monkeypatch: pytest.MonkeyPatch):
    """Set up gateway client with explicit test configuration and keys."""
    test_key = "argus-secret-test-key-67890"
    monkeypatch.setenv("ARGUS_GATEWAY_API_KEY", test_key)
    monkeypatch.setenv("ARGUS_TEST_MODE", "1")

    cfg = GatewayConfig(
        gateway_api_key=test_key,
        providers=[
            ProviderConfig(
                name="test_openai",
                model="gpt-4o",
                api_key="upstream-fake-key",
                base_url="https://api.openai.com/v1",
            )
        ],
        tiers={
            "auto": TierDefinition(provider="test_openai", model="gpt-4o"),
            "cheap": TierDefinition(provider="test_openai", model="gpt-4o-mini"),
        },
        budget=BudgetConfig(per_session_usd=0.20, per_task_usd=0.10, on_exhausted="fail_fast"),
    )
    init_gateway(cfg)
    client = TestClient(app)
    return client, test_key


# ============================================================================
# 1. Complete Route Inventory and Authentication Boundary
# ============================================================================

def test_route_inventory_authentication_boundary(deep_gateway_client):
    """Every sensitive or inference route enforces authentication; health check leaks no telemetry."""
    client, valid_key = deep_gateway_client
    auth_header = {"Authorization": f"Bearer {valid_key}"}

    # 1. /v1/chat/completions requires auth
    res = client.post("/v1/chat/completions", json={"model": "argus/auto", "messages": []})
    assert res.status_code == 401

    # 2. /v1/messages requires auth
    res = client.post("/v1/messages", json={"model": "argus/auto", "messages": []})
    assert res.status_code == 401

    # 3. /v1/models requires auth
    res = client.get("/v1/models")
    assert res.status_code == 401
    assert "Invalid or missing Argus Gateway" in res.json()["error"]["message"]

    res_authed = client.get("/v1/models", headers=auth_header)
    assert res_authed.status_code == 200
    assert res_authed.json()["object"] == "list"

    # 4. /health unauthenticated probe leaks no telemetry
    res_health_unauth = client.get("/health")
    assert res_health_unauth.status_code == 200
    data_unauth = res_health_unauth.json()
    assert data_unauth["status"] == "healthy"
    # Sensitive telemetry must NOT be present
    assert "session_spend_usd" not in data_unauth
    assert "configured_providers" not in data_unauth
    assert "active_tiers" not in data_unauth

    # 5. /health authenticated probe returns full telemetry
    res_health_authed = client.get("/health", headers=auth_header)
    assert res_health_authed.status_code == 200
    data_authed = res_health_authed.json()
    assert "session_spend_usd" in data_authed
    assert "configured_providers" in data_authed
    assert "active_tiers" in data_authed


# ============================================================================
# 2. Direct ASGI Startup Fail-Closed Behavior
# ============================================================================

@pytest.mark.asyncio
async def test_direct_asgi_startup_without_key_fails_closed(monkeypatch: pytest.MonkeyPatch):
    """Direct ASGI lifespan startup raises RuntimeError if ARGUS_GATEWAY_API_KEY is unset."""
    monkeypatch.delenv("ARGUS_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("ARGUS_TEST_MODE", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    # Empty config with no key
    init_gateway(GatewayConfig(gateway_api_key=None))

    with pytest.raises(RuntimeError, match="ARGUS_GATEWAY_API_KEY environment variable is required"):
        async with lifespan(app):
            pass


# ============================================================================
# 3. Host, Origin & Testserver Boundary
# ============================================================================

def test_testserver_host_permitted_only_in_test_mode(monkeypatch: pytest.MonkeyPatch):
    """Host 'testserver' is only permitted when running under test environment."""
    test_key = "test-key"
    monkeypatch.setenv("ARGUS_GATEWAY_API_KEY", test_key)
    init_gateway(GatewayConfig(gateway_api_key=test_key))

    # In test mode: testserver is accepted
    monkeypatch.setenv("ARGUS_TEST_MODE", "1")
    client = TestClient(app)
    res = client.get("/health", headers={"Host": "testserver"})
    assert res.status_code == 200

    # In production mode (no test env flags): testserver must be rejected
    monkeypatch.delenv("ARGUS_TEST_MODE", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    res_prod = client.get("/health", headers={"Host": "testserver"})
    assert res_prod.status_code == 400
    assert "rejected" in res_prod.json()["error"]["message"]


# ============================================================================
# 4. Multi-Client Rate Limiting on Localhost
# ============================================================================

def test_localhost_multi_client_rate_limiting_isolation():
    """Different authenticated clients or local tools on 127.0.0.1 have independent rate buckets."""
    limiter = GatewayRateLimiter(max_requests_per_minute=5)

    # Two distinct local applications connecting via loopback
    app_continue = "auth:sha256_token1_hash:continue-extension"
    app_openwebui = "auth:sha256_token2_hash:open-webui"

    # App 1 makes 5 requests, filling its quota
    for _ in range(5):
        assert limiter.allow_request(app_continue) is True

    # App 1 6th request is rate limited
    assert limiter.allow_request(app_continue) is False

    # App 2 makes a request immediately: MUST be allowed (no starvation across localhost tools!)
    assert limiter.allow_request(app_openwebui) is True


# ============================================================================
# 5. Budget Concurrency, Estimator Pricing, and Spending Ceiling
# ============================================================================

def test_dynamic_cost_estimation_uses_model_pricing():
    """Cost estimation incorporates actual model registry token rates and max_tokens."""
    # Register test model
    MODEL_REGISTRY["argus/expensive-test"] = ModelCost(input_cost_per_m=10.0, output_cost_per_m=30.0)
    try:
        body = {
            "messages": [{"role": "user", "content": "A" * 4000}],  # ~1000 prompt tokens
            "max_tokens": 2000,
        }
        # Model rate: (1000/1M * 10) + (2000/1M * 30) = 0.01 + 0.06 = $0.07
        est = estimate_request_cost(body, model="argus/expensive-test")
        assert est == pytest.approx(0.07, abs=0.005)
    finally:
        del MODEL_REGISTRY["argus/expensive-test"]


def test_concurrent_budget_reservations_cannot_exceed_ceiling():
    """Concurrent requests cannot bypass session budget ceiling through race conditions."""
    # Session budget of $0.15; each reservation is $0.05
    bg = BudgetGuard(BudgetConfig(per_session_usd=0.15, per_task_usd=0.15))

    successful_reservations = []
    rejected_reservations = []
    lock = threading.Lock()

    def try_reserve(idx: int):
        ok, msg, res_id = bg.reserve(f"task_{idx}", estimated_amount=0.05)
        with lock:
            if ok:
                successful_reservations.append(res_id)
            else:
                rejected_reservations.append(msg)

    # Launch 10 concurrent reservation attempts
    threads = [threading.Thread(target=try_reserve, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly 3 reservations should succeed (3 * 0.05 = 0.15)
    assert len(successful_reservations) == 3
    # Remaining 7 attempts must be rejected
    assert len(rejected_reservations) == 7
    # Session limit must not be breached
    assert bg._reserved_session_usd == pytest.approx(0.15, abs=0.001)


def test_budget_uncertain_outcome_reconciliation():
    """Uncertain execution (e.g. timeout) converts reservation to liability and prevents overspend."""
    bg = BudgetGuard(BudgetConfig(per_session_usd=0.10, per_task_usd=0.10))

    # Reserve $0.05
    ok, _, res_id = bg.reserve("run_1", estimated_amount=0.05)
    assert ok is True

    # Record uncertain outcome (timeout)
    bg.record_uncertain(reservation_id=res_id, run_id="run_1", model="gpt-4o", estimated_amount=0.05)
    assert bg.get_uncertain_spend() == pytest.approx(0.05, abs=0.001)
    assert bg._reserved_session_usd == 0.0

    # Another $0.05 reservation succeeds (0.05 uncertain + 0.05 reserved = 0.10)
    ok2, _, res_id2 = bg.reserve("run_2", estimated_amount=0.05)
    assert ok2 is True

    # A 3rd reservation exceeds budget ceiling and must fail
    ok3, msg3, _ = bg.reserve("run_3", estimated_amount=0.05)
    assert ok3 is False
    assert "Session budget limit" in msg3
