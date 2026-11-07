"""Integration tests for Argus Gateway Anthropic Messages Endpoint (Milestone A3)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch
from starlette.testclient import TestClient

from argus.gateway.config import GatewayConfig, ProviderConfig, TierDefinition
from argus.gateway.server import app, init_gateway


def test_gateway_health_reports_dual_protocols():
    """Verify /health advertises both OpenAI and Anthropic protocols."""
    cfg = GatewayConfig(
        providers=[
            ProviderConfig(name="mock_groq", base_url="https://api.mock.com/v1", api_key="sk-test-123")
        ],
        tiers={
            "cheap": TierDefinition(provider="mock_groq", model="openai/gpt-oss-20b"),
        },
    )
    init_gateway(config=cfg, workspace=".")
    client = TestClient(app)

    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "protocols" in data
    assert "openai/chat/completions" in data["protocols"]
    assert "anthropic/messages" in data["protocols"]


def test_anthropic_messages_endpoint_sync_mock():
    """Verify /v1/messages accepts Anthropic format and returns Anthropic structure."""
    cfg = GatewayConfig(
        providers=[
            ProviderConfig(name="mock_groq", base_url="https://api.mock.com/v1", api_key="sk-test-123")
        ],
        tiers={
            "cheap": TierDefinition(provider="mock_groq", model="openai/gpt-oss-20b"),
        },
    )
    init_gateway(config=cfg, workspace=".")
    client = TestClient(app)

    # Mock the upstream httpx post response
    mock_openai_response = {
        "id": "chatcmpl-mock-123",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "openai/gpt-oss-20b",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "def test_ok(): pass",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 15, "completion_tokens": 8, "total_tokens": 23},
    }

    import httpx
    real_mock_resp = httpx.Response(status_code=200, json=mock_openai_response)

    with patch("httpx.AsyncClient.post", return_value=real_mock_resp):
        payload = {
            "model": "argus/auto",
            "messages": [{"role": "user", "content": "Write a test"}],
            "max_tokens": 100,
            "stream": False,
        }
        res = client.post("/v1/messages", json=payload, headers={"x-api-key": "argus-local"})
        assert res.status_code == 200
        data = res.json()
        assert data["type"] == "message"
        assert data["role"] == "assistant"
        assert data["content"][0]["text"] == "def test_ok(): pass"
        assert data["model"] == "argus/auto"
        assert data["usage"]["input_tokens"] == 15
        assert data["usage"]["output_tokens"] == 8
