"""Regression coverage for Anthropic Messages routing to the Antigravity host (#44).

All host inference is mocked: these tests must not invoke the real Antigravity
CLI, any model provider, or a network request.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from starlette.testclient import TestClient

from argus.gateway import server
from argus.gateway.config import BudgetConfig, GatewayConfig, ProviderConfig, TierDefinition
from argus.gateway.server import app, init_gateway
from argus.providers.adapter import CompletionResponse

HOST_MODEL = "host-model"
SYSTEM_BLOCKS = [{"type": "text", "text": "You are a careful assistant."}]
MESSAGES = [{"role": "user", "content": [{"type": "text", "text": "Say hello."}]}]


@pytest.fixture
def host_gateway(tmp_path):
    cfg = GatewayConfig(
        providers=[ProviderConfig(name="antigravity", base_url="host://antigravity-cli")],
        tiers={"cheap": TierDefinition(provider="antigravity", model=HOST_MODEL)},
        budget=BudgetConfig(per_task_usd=1.0, per_session_usd=1.0),
    )
    init_gateway(config=cfg, workspace=tmp_path)
    yield
    assert server._budget_guard is not None
    assert not server._budget_guard._active_reservations


def _request(*, stream=False, system=None):
    return {
        "model": "argus/cheap",
        "system": SYSTEM_BLOCKS if system is None else system,
        "messages": MESSAGES,
        "max_tokens": 128,
        "stream": stream,
    }


def test_host_sync_returns_anthropic_message_and_never_uses_http(host_gateway):
    response = CompletionResponse(
        content="Hello from host.", prompt_tokens=12, completion_tokens=4,
        duration_ms=3, model=HOST_MODEL,
    )
    with (
        patch("argus.providers.antigravity.AntigravityHostAdapter.chat_completion", return_value=response) as host,
        patch("httpx.AsyncClient.post", side_effect=AssertionError("host:// must not be passed to httpx")),
    ):
        result = TestClient(app).post("/v1/messages", json=_request())

    assert result.status_code == 200, result.text
    body = result.json()
    assert body["type"] == "message"
    assert body["content"] == [{"type": "text", "text": "Hello from host."}]
    assert body["usage"] == {"input_tokens": 12, "output_tokens": 4}
    assert body["model"] == "argus/cheap"
    sent = host.call_args.kwargs["messages"]
    assert sent[0] == {"role": "system", "content": "You are a careful assistant."}
    assert sent[1] == {"role": "user", "content": "Say hello."}


def test_host_stream_returns_anthropic_events_and_never_uses_http(host_gateway):
    def deltas(*_args, **_kwargs):
        yield "Hello"
        yield " host."

    with (
        patch("argus.providers.antigravity.AntigravityHostAdapter.stream_chat_completion", side_effect=deltas) as host,
        patch("httpx.AsyncClient.post", side_effect=AssertionError("host:// must not be passed to httpx")),
    ):
        result = TestClient(app).post("/v1/messages", json=_request(stream=True))

    assert result.status_code == 200, result.text
    assert "event: message_start" in result.text
    assert "event: content_block_start" in result.text
    assert "event: content_block_delta" in result.text
    assert "event: message_delta" in result.text
    assert "event: message_stop" in result.text
    assert "Hello" in result.text and " host." in result.text
    assert host.called
    assert "chat.completion.chunk" not in result.text


def test_host_error_has_anthropic_error_envelope_and_releases_budget(host_gateway):
    with patch(
        "argus.providers.antigravity.AntigravityHostAdapter.chat_completion",
        side_effect=RuntimeError("offline test host failure"),
    ):
        response = TestClient(app).post("/v1/messages", json=_request())

    assert response.status_code == 502
    payload = response.json()
    assert payload["type"] == "error"
    assert payload["error"]["code"] == "antigravity_execution_failed"


def test_anthropic_system_blocks_are_normalized_before_auto_routing(tmp_path):
    cfg = GatewayConfig(
        providers=[ProviderConfig(name="antigravity", base_url="host://antigravity-cli")],
        tiers={
            "cheap": TierDefinition(provider="antigravity", model=HOST_MODEL),
            "strong": TierDefinition(provider="antigravity", model=HOST_MODEL),
            "frontier": TierDefinition(provider="antigravity", model=HOST_MODEL),
        },
        budget=BudgetConfig(per_task_usd=1.0, per_session_usd=1.0),
    )
    init_gateway(config=cfg, workspace=tmp_path)
    response = CompletionResponse(
        content="done", prompt_tokens=1, completion_tokens=1, duration_ms=1, model=HOST_MODEL
    )
    with (
        patch("argus.providers.antigravity.AntigravityHostAdapter.chat_completion", return_value=response),
        patch.object(server._router.classifier, "classify", wraps=server._router.classifier.classify) as classify,
    ):
        request = _request()
        request["model"] = "argus/auto"
        result = TestClient(app).post("/v1/messages", json=request)

    assert result.status_code == 200, result.text
    assert classify.called
    prompt = classify.call_args.kwargs["prompt"]
    assert isinstance(prompt, str)
    assert "You are a careful assistant." in prompt
    assert "Say hello." in prompt
    assert not server._budget_guard._active_reservations


def test_malformed_system_blocks_fail_before_budget_is_reserved(host_gateway):
    body = _request()
    body["system"] = [{"type": "text", "text": ["not a string"]}]
    response = TestClient(app).post("/v1/messages", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_unexpected_router_error_releases_budget(host_gateway):
    with patch.object(server._router, "resolve", side_effect=RuntimeError("routing failed")):
        response = TestClient(app, raise_server_exceptions=False).post("/v1/messages", json=_request())

    assert response.status_code == 500


def test_anthropic_system_blocks_route_over_http_without_leaking_budget(tmp_path):
    cfg = GatewayConfig(
        providers=[ProviderConfig(name="mock", base_url="https://api.mock.com/v1", api_key="mock-key")],
        tiers={"cheap": TierDefinition(provider="mock", model="gpt-4o")},
        budget=BudgetConfig(per_task_usd=1.0, per_session_usd=1.0),
    )
    init_gateway(config=cfg, workspace=tmp_path)
    upstream = httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "choices": [{"message": {"role": "assistant", "content": "Hello."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        },
        request=httpx.Request("POST", "https://api.mock.com/v1/chat/completions"),
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=upstream) as post:
        result = TestClient(app).post("/v1/messages", json=_request())

    assert result.status_code == 200, result.text
    assert result.json()["content"] == [{"type": "text", "text": "Hello."}]
    sent = post.call_args.kwargs["json"]["messages"]
    assert sent[0]["content"] == "You are a careful assistant."
    assert sent[1]["content"] == "Say hello."
    assert not server._budget_guard._active_reservations
