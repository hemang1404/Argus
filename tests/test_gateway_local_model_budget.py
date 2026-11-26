"""Unpriced local models reconcile at $0.00 after the request, as the pre-flight check prices them (#43).

A model absent from MODEL_REGISTRY passes the pre-flight check when its upstream is a local provider
(Ollama, LM Studio) or a loopback host, but the commit after the request used to price it by name only,
raise UnpricedModelError and charge the whole reservation as uncertain. A few turns then exhausted the
budget and the next free local request got HTTP 429 (budget_exceeded).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from starlette.testclient import TestClient

from argus.gateway import server
from argus.gateway.budget import BudgetGuard
from argus.gateway.config import BudgetConfig, GatewayConfig, ProviderConfig, TierDefinition
from argus.gateway.server import app, init_gateway
from argus.gateway.telemetry import TelemetryRegistry

LOCAL_MODEL = "llama3.2:latest"  # not in MODEL_REGISTRY
TURNS = 6
USAGE = {"prompt_tokens": 40_000, "completion_tokens": 8_000, "total_tokens": 48_000}


@pytest.fixture(params=[("ollama", "http://localhost:11434/v1"), ("my-box", "http://127.0.0.1:1234/v1")])
def local_gateway(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A gateway whose every tier is a local model: free by provider name, or by loopback host."""
    monkeypatch.chdir(tmp_path)
    TelemetryRegistry._instance = None
    TelemetryRegistry.get(workspace_root=tmp_path).clear()
    name, base_url = request.param
    tier = TierDefinition(provider=name, model=LOCAL_MODEL)
    cfg = GatewayConfig(
        providers=[ProviderConfig(name=name, base_url=base_url, api_key="unused")],
        tiers={"cheap": tier, "strong": tier, "frontier": tier},
        # Small enough that charging the reservation as uncertain blocks within a few turns.
        budget=BudgetConfig(per_session_usd=0.05, per_task_usd=0.05),
    )
    init_gateway(config=cfg, workspace=tmp_path)
    yield base_url
    TelemetryRegistry._instance = None


def _chat(content: str = "Explain this function.", **extra) -> dict:
    return {"model": "argus/auto", "messages": [{"role": "user", "content": content}], **extra}


@pytest.fixture
def uncertain_calls(local_gateway, monkeypatch: pytest.MonkeyPatch) -> list:
    """Every record_uncertain call: a priced local model must reconcile without one."""
    calls = []
    original = server._budget_guard.record_uncertain

    def spy(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(server._budget_guard, "record_uncertain", spy)
    return calls


def _assert_free(uncertain_calls: list):
    assert uncertain_calls == []
    assert server._budget_guard.get_session_spend() == 0.0
    assert server._budget_guard.get_uncertain_spend() == 0.0


def test_sync_turns_to_an_unpriced_local_model_cost_nothing(local_gateway, uncertain_calls):
    upstream = httpx.Response(
        200,
        json={
            "id": "chatcmpl-local",
            "object": "chat.completion",
            "model": LOCAL_MODEL,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "It adds."}, "finish_reason": "stop"}],
            "usage": USAGE,
        },
        request=httpx.Request("POST", f"{local_gateway}/chat/completions"),
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=upstream):
        client = TestClient(app)
        for _ in range(TURNS):
            resp = client.post("/v1/chat/completions", json=_chat())
            assert resp.status_code == 200, resp.text
    _assert_free(uncertain_calls)


def test_streaming_turns_to_an_unpriced_local_model_cost_nothing(local_gateway, uncertain_calls):
    async def lines():
        yield 'data: {"choices":[{"delta":{"content":"It adds."}}]}'
        yield 'data: {"choices":[],"usage":{"prompt_tokens":40000,"completion_tokens":8000,"total_tokens":48000}}'
        yield "data: [DONE]"

    def streamed(*_args, **_kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.aiter_lines = lines
        resp.__aenter__.return_value = resp
        return resp

    with patch("httpx.AsyncClient.send", side_effect=streamed):
        client = TestClient(app)
        for _ in range(TURNS):
            resp = client.post("/v1/chat/completions", json=_chat(stream=True))
            assert resp.status_code == 200, resp.text
            assert "budget_exceeded" not in resp.text
            assert "It adds." in resp.text
    _assert_free(uncertain_calls)


def test_anthropic_sync_turns_to_an_unpriced_local_model_cost_nothing(local_gateway, uncertain_calls):
    upstream = httpx.Response(
        200,
        json={
            "id": "chatcmpl-local",
            "object": "chat.completion",
            "model": LOCAL_MODEL,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "It adds."}, "finish_reason": "stop"}],
            "usage": USAGE,
        },
        request=httpx.Request("POST", f"{local_gateway}/chat/completions"),
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=upstream):
        client = TestClient(app)
        body = {"model": "argus/auto", "max_tokens": 256, "messages": [{"role": "user", "content": "Explain this."}]}
        for _ in range(TURNS):
            resp = client.post("/v1/messages", json=body)
            assert resp.status_code == 200, resp.text
    _assert_free(uncertain_calls)


def test_commit_still_fails_closed_for_an_unpriced_remote_model():
    guard = BudgetGuard(BudgetConfig(per_session_usd=10.0, per_task_usd=10.0))
    for provider, base_url in [("some-cloud", "https://api.example.com/v1"), (None, None)]:
        before = guard.get_uncertain_spend()
        guard.commit(None, "run-1", "mystery-model", 1000, 1000, provider=provider, base_url=base_url)
        assert guard.get_session_spend() == 0.0
        assert guard.get_uncertain_spend() >= before  # charged as uncertain, never priced at a guess
    # A local upstream for the same unknown model reconciles at zero.
    assert guard.commit(None, "run-1", "mystery-model", 1000, 1000, provider="lmstudio") == 0.0
    assert guard.commit(None, "run-1", "mystery-model", 1000, 1000, base_url="http://[::1]:8080/v1") == 0.0
