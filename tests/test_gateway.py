"""Tests for Argus Local Proxy Gateway (Milestone A2).

Verifies:
1. Health endpoint telemetry
2. OpenAI-compatible /v1/models advertisement
3. GatewayRouter alias resolution
4. Thread-safe BudgetGuard enforcement
5. In-memory streaming patch healer repairing broken search/replace blocks
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from starlette.testclient import TestClient

from argus.gateway.budget import BudgetGuard
from argus.gateway.config import BudgetConfig, GatewayConfig, ProviderConfig, TierDefinition
from argus.gateway.interceptor import StreamInterceptor, StreamPatchHealer
from argus.gateway.router import GatewayRouter
from argus.gateway.server import app, init_gateway


def test_gateway_health_and_models():
    """Verify /health and /v1/models return OpenAI-compliant structures."""
    cfg = GatewayConfig(
        providers=[
            ProviderConfig(name="mock_groq", base_url="https://api.mock.com/v1", api_key="sk-test-123")
        ],
        tiers={
            "cheap": TierDefinition(provider="mock_groq", model="openai/gpt-oss-20b"),
            "strong": TierDefinition(provider="mock_groq", model="openai/gpt-oss-120b"),
            "frontier": TierDefinition(provider="mock_groq", model="mock-frontier-v1"),
        },
    )
    init_gateway(config=cfg, workspace=".")

    client = TestClient(app)

    # 1. /health
    h_resp = client.get("/health")
    assert h_resp.status_code == 200
    h_data = h_resp.json()
    assert h_data["status"] == "healthy"
    assert "mock_groq" in h_data["configured_providers"]
    assert "cheap" in h_data["active_tiers"]

    # 2. /v1/models
    m_resp = client.get("/v1/models")
    assert m_resp.status_code == 200
    m_data = m_resp.json()
    model_ids = [m["id"] for m in m_data["data"]]
    assert "argus/auto" in model_ids
    assert "argus/cheap" in model_ids
    assert "argus/strong" in model_ids
    assert "argus/frontier" in model_ids
    assert "openai/gpt-oss-20b" in model_ids


def test_gateway_router_resolution():
    """Verify tier alias mapping and pass-through fallback."""
    cfg = GatewayConfig(
        providers=[
            ProviderConfig(name="deepseek", base_url="https://api.deepseek.com/v1", api_key="sk-deepseek"),
            ProviderConfig(name="openai", base_url="https://api.openai.com/v1", api_key="sk-openai"),
        ],
        tiers={
            "cheap": TierDefinition(provider="deepseek", model="deepseek-chat"),
            "strong": TierDefinition(provider="deepseek", model="deepseek-coder"),
            "frontier": TierDefinition(provider="openai", model="gpt-4o"),
        },
    )
    router = GatewayRouter(cfg)

    # Alias: argus/auto -> cheap
    t1 = router.resolve("argus/auto")
    assert t1.upstream_model == "deepseek-chat"
    assert t1.provider_name == "deepseek"
    assert t1.is_pinned is False

    # Alias: argus/frontier -> frontier
    t2 = router.resolve("argus/frontier")
    assert t2.upstream_model == "gpt-4o"
    assert t2.provider_name == "openai"
    assert t2.is_pinned is False

    # Pinned: custom model
    t3 = router.resolve("claude-3-7-sonnet")
    assert t3.upstream_model == "claude-3-7-sonnet"
    assert t3.is_pinned is True


def test_budget_guard_enforcement():
    """Verify task and session budget enforcement."""
    b_cfg = BudgetConfig(per_task_usd=0.01, per_session_usd=0.05, on_exhausted="fail_fast")
    guard = BudgetGuard(b_cfg)

    # Initial check passes
    ok, _ = guard.check_budget("task-1")
    assert ok is True

    # Record small spend
    guard.record_usage("task-1", "openai/gpt-oss-20b", prompt_tokens=1000, completion_tokens=1000)
    assert guard.get_task_spend("task-1") > 0.0

    # Exhaust task budget without exceeding session budget
    guard.record_usage("task-1", "openai/gpt-oss-20b", prompt_tokens=30000, completion_tokens=30000)
    ok, err = guard.check_budget("task-1")
    assert ok is False
    assert "Task budget limit" in err

    # Another task still has budget if session spend is within limit
    ok2, _ = guard.check_budget("task-2")
    assert ok2 is True


def test_streaming_patch_healer_in_memory():
    """Verify that in-memory patch healer fixes broken whitespace and aligns with disk file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        target_file = tmp_path / "calc.py"
        target_file.write_text(
            "def calculate(a: int, b: int) -> int:\n"
            "    # Exact file contents with 4-space indent\n"
            "    total = a + b\n"
            "    return total\n",
            encoding="utf-8",
        )

        healer = StreamPatchHealer(workspace=tmp_path, enabled=True)
        interceptor = StreamInterceptor(healer)

        # Simulate LLM output with bad indentation / whitespace in search block
        broken_patch = (
            "Here is the fix for your issue:\n"
            "<<<<<<< SEARCH\n"
            "def calculate(a: int, b: int) -> int:\n"
            "  # Exact file contents with 4-space indent\n"
            "  total = a + b\n"
            "=======\n"
            "def calculate(a: int, b: int) -> int:\n"
            "    # Fixed total\n"
            "    total = a + b + 10\n"
            ">>>>>>> REPLACE\n"
            "Done!"
        )

        # Feed chunk by chunk through interceptor
        chunks = [broken_patch[:20], broken_patch[20:70], broken_patch[70:]]
        emitted = []
        for ch in chunks:
            out = interceptor.process_chunk(ch)
            if out:
                emitted.append(out)
        flushed = interceptor.flush()
        if flushed:
            emitted.append(flushed)

        full_output = "".join(emitted)
        assert "Here is the fix" in full_output
        assert "Done!" in full_output
        assert "<<<<<<< SEARCH" in full_output
        assert ">>>>>>> REPLACE" in full_output

        # Verify the search block was healed to have the real 4-space indentation!
        assert "    # Exact file contents with 4-space indent\n    total = a + b" in full_output
