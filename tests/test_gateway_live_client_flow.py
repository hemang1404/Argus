"""End-to-End Client Integration Tests.

Validates that external AI developer tools (Aider, Cursor, Claude Code, OpenCode):
1. Connect directly to Argus Gateway via OpenAI and Anthropic protocol endpoints.
2. Are dynamically detected via User-Agent and headers.
3. Have requests dynamically routed based on prompt complexity heuristics.
4. Correctly track token economics and calculate realized alpha savings.
5. Persist telemetry to disk for the TUI Stats modal.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from starlette.testclient import TestClient

from argus.gateway.config import BudgetConfig, GatewayConfig, ProviderConfig, TierDefinition
from argus.gateway.server import app, init_gateway
from argus.gateway.telemetry import TelemetryRegistry, detect_client
from argus.tui.modals.stats import StatsModal


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Ensure clean isolated telemetry environment for every test."""
    monkeypatch.chdir(tmp_path)
    TelemetryRegistry._instance = None
    registry = TelemetryRegistry.get(workspace_root=tmp_path)
    registry.clear()

    cfg = GatewayConfig(
        providers=[
            ProviderConfig(name="mock_deepseek", base_url="https://api.deepseek.com/v1", api_key="sk-mock-cheap"),
            ProviderConfig(name="mock_openai", base_url="https://api.openai.com/v1", api_key="sk-mock-frontier"),
        ],
        tiers={
            "cheap": TierDefinition(provider="mock_deepseek", model="deepseek-chat"),
            "strong": TierDefinition(provider="mock_deepseek", model="deepseek-coder"),
            "frontier": TierDefinition(provider="mock_openai", model="gpt-4o"),
        },
        budget=BudgetConfig(per_session_usd=1.00, per_task_usd=0.50),
    )
    init_gateway(config=cfg, workspace=tmp_path)
    yield tmp_path
    TelemetryRegistry._instance = None


def test_detect_client_variations():
    """Verify external agent identification across various headers."""
    assert detect_client(user_agent="aider/0.74.0") == "aider"
    assert detect_client(user_agent="Cursor/0.45.0 Mozilla/5.0") == "cursor"
    assert detect_client(user_agent="opencode/0.1.2") == "opencode"
    assert detect_client(user_agent="claude-code/0.1.0") == "claude-code"
    assert detect_client(user_agent="Continue/0.8.0") == "continue"
    assert detect_client(user_agent="antigravity/2.0") == "antigravity"
    assert detect_client(client_header="my-custom-cli") == "my-custom-cli"
    assert detect_client(user_agent="curl/7.68.0") == "api-client"
    assert detect_client(user_agent="unknown-bot/1.0") == "external-client"


@pytest.mark.asyncio
async def test_aider_flow_routes_tier1_cheap():
    """Simulate Aider sending a low-complexity typo fix via OpenAI chat endpoint."""
    mock_upstream_response = httpx.Response(
        status_code=200,
        json={
            "id": "chatcmpl-aider-1",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "deepseek-chat",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Fixed spelling error in docstring.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 25,
                "total_tokens": 145,
            },
        },
        request=httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions"),
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_upstream_response

        client = TestClient(app)
        headers = {
            "User-Agent": "aider/0.74.0 (Python 3.11)",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "argus/auto",
            "messages": [
                {"role": "user", "content": "Please fix typo in docstring and explain the change."},
            ],
        }

        resp = client.post("/v1/chat/completions", headers=headers, json=payload)
        assert resp.status_code == 200
        data = resp.json()

        # Upstream request verified: routed to Tier 1 (deepseek-chat)
        assert mock_post.called
        call_kwargs = mock_post.call_args.kwargs
        assert call_kwargs["json"]["model"] == "deepseek-chat"
        assert "Authorization" in call_kwargs["headers"]

        # Caller sees the model alias they requested
        assert data["model"] == "argus/auto"
        assert data["choices"][0]["message"]["content"] == "Fixed spelling error in docstring."

        # Verify telemetry recorded
        summary = TelemetryRegistry.get().get_summary()
        assert summary.total_calls == 1
        assert summary.active_client == "aider"
        assert summary.tier_counts["cheap"] == 1
        assert summary.prompt_tokens == 120
        assert summary.completion_tokens == 25
        assert summary.session_spend_usd > 0
        assert summary.alpha_saved_usd > 0
        assert summary.alpha_percent > 0


@pytest.mark.asyncio
async def test_cursor_flow_routes_tier3_frontier():
    """Simulate Cursor sending a high-complexity architectural refactoring prompt."""
    mock_upstream_response = httpx.Response(
        status_code=200,
        json={
            "id": "chatcmpl-cursor-1",
            "object": "chat.completion",
            "created": 1700000001,
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Refactored async architecture to prevent deadlock.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 1500,
                "completion_tokens": 400,
                "total_tokens": 1900,
            },
        },
        request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_upstream_response

        client = TestClient(app)
        headers = {
            "User-Agent": "Cursor/0.45.0 (Windows NT 10.0; Win64; x64)",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "argus/auto",
            "messages": [
                {
                    "role": "user",
                    "content": "Redesign system architecture with asyncio concurrency and deadlock prevention across multiple files.",
                },
            ],
        }

        resp = client.post("/v1/chat/completions", headers=headers, json=payload)
        assert resp.status_code == 200

        # High difficulty triggers tier 3: gpt-4o
        assert mock_post.called
        call_kwargs = mock_post.call_args.kwargs
        assert call_kwargs["json"]["model"] == "gpt-4o"

        summary = TelemetryRegistry.get().get_summary()
        assert summary.total_calls == 1
        assert summary.active_client == "cursor"
        assert summary.tier_counts["frontier"] == 1


@pytest.mark.asyncio
async def test_claude_code_anthropic_messages_flow():
    """Simulate Claude Code using /v1/messages Anthropic adapter."""
    mock_upstream_response = httpx.Response(
        status_code=200,
        json={
            "id": "chatcmpl-claude-1",
            "object": "chat.completion",
            "created": 1700000002,
            "model": "openai/gpt-oss-20b",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Here is the implemented function.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 300,
                "completion_tokens": 80,
                "total_tokens": 380,
            },
        },
        request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions"),
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_upstream_response

        client = TestClient(app)
        headers = {
            "User-Agent": "claude-code/0.1.0",
            "x-api-key": "test-key-ignored-by-local-proxy",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "argus/auto",
            "system": "You are a concise programming assistant.",
            "messages": [
                {"role": "user", "content": "Write a python helper to validate UUIDs."},
            ],
            "max_tokens": 512,
        }

        resp = client.post("/v1/messages", headers=headers, json=payload)
        assert resp.status_code == 200
        data = resp.json()

        # Anthropic response format contract check
        assert data["type"] == "message"
        assert data["role"] == "assistant"
        assert len(data["content"]) > 0
        assert data["content"][0]["type"] == "text"
        assert "implemented function" in data["content"][0]["text"]
        assert data["usage"]["input_tokens"] == 300
        assert data["usage"]["output_tokens"] == 80

        # Telemetry verification
        summary = TelemetryRegistry.get().get_summary()
        assert summary.active_client == "claude-code"
        assert summary.total_calls == 1


@pytest.mark.asyncio
async def test_session_persistence_and_stats_modal(tmp_path: Path):
    """Verify live traffic updates disk files and StatsModal displays truthful metrics."""
    mock_resp = httpx.Response(
        status_code=200,
        json={
            "id": "chatcmpl-test",
            "choices": [{"message": {"role": "assistant", "content": "Done."}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        },
        request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions"),
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp

        client = TestClient(app)

        # 1. Zero state test: Modal before any calls
        zero_modal = StatsModal()
        zero_table = zero_modal._build_table()
        assert zero_modal.total_calls == 0
        # Check that table contains status row
        metric_cells = [str(c) for c in zero_table.columns[0]._cells]
        assert "Status" in metric_cells
        assert "Active Client" in metric_cells

        # 2. Make an Aider call
        client.post(
            "/v1/chat/completions",
            headers={"User-Agent": "aider/0.74.0"},
            json={"model": "argus/auto", "messages": [{"role": "user", "content": "fix typo"}]},
        )

        # 3. Verify .argus persistence files exist
        session_file = tmp_path / ".argus" / "session.json"
        events_file = tmp_path / ".argus" / "telemetry_events.jsonl"
        assert session_file.exists()
        assert events_file.exists()

        session_data = json.loads(session_file.read_text(encoding="utf-8"))
        assert session_data["total_calls"] == 1
        assert session_data["active_client"] == "aider"

        # 4. Verify /health reflects telemetry
        h_resp = client.get("/health")
        assert h_resp.status_code == 200
        h_data = h_resp.json()
        assert h_data["total_calls"] == 1
        assert h_data["active_client"] == "aider"

        # 5. Verify StatsModal renders live metrics
        active_modal = StatsModal()
        assert active_modal.total_calls == 1
        assert active_modal.active_client == "aider"
        active_table = active_modal._build_table()
        active_cells = [str(cell) for col in active_table.columns for cell in col._cells]
        assert any("aider" in c for c in active_cells)
        assert any("Tier 1 (Cheap 8B)" in c for c in active_cells)
