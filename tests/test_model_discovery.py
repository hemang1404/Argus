"""Tests for dynamic model discovery and tier resolution."""

from unittest.mock import patch


from argus.config import ArgusConfig
from argus.providers.discovery import (
    DiscoveredModel,
    discover_all_models,
    resolve_dynamic_tiers,
)
from argus.tui.modals.config import ConfigModal
from argus.tui.modals.policy import PolicyModal


def test_discovered_model_display_line():
    m = DiscoveredModel(
        id="gemini-3.8-flash-medium",
        name="Gemini 3.8 Flash",
        provider="antigravity",
        tier="fast",
        is_available=True,
        status_label="Ready (Host Native)",
        status_tag="ready_host",
        description="Host Native · Fast Latency",
    )
    assert "[bold #3fb950]● Ready (Host)[/]" in m.display_line
    assert "gemini-3.8-flash-medium" in m.display_line
    assert "Host Native · Fast Latency" in m.display_line
    assert m.model_id == "gemini-3.8-flash-medium"


def test_resolve_dynamic_tiers_with_host_models():
    models = [
        DiscoveredModel(
            id="gemini-3.8-flash-low",
            name="Gemini 3.8 Flash (Low)",
            provider="antigravity",
            tier="fast",
            is_available=True,
            status_label="Ready",
            status_tag="ready_host",
            description="Fast",
        ),
        DiscoveredModel(
            id="claude-sonnet-4-6",
            name="Claude Sonnet 4.6",
            provider="antigravity",
            tier="strong",
            is_available=True,
            status_label="Ready",
            status_tag="ready_host",
            description="Strong",
        ),
        DiscoveredModel(
            id="claude-opus-4-6-thinking",
            name="Claude Opus 4.6",
            provider="antigravity",
            tier="frontier",
            is_available=True,
            status_label="Ready",
            status_tag="ready_host",
            description="Frontier",
        ),
    ]
    tiers = resolve_dynamic_tiers(models)
    assert tiers["t1"]["model"] == "gemini-3.8-flash-low"
    assert tiers["t2"]["model"] == "claude-sonnet-4-6"
    assert tiers["t3"]["model"] == "claude-opus-4-6-thinking"


def test_resolve_dynamic_tiers_fallback_when_none_available():
    tiers = resolve_dynamic_tiers([])
    assert "t1" in tiers and tiers["t1"]["model"]
    assert "t2" in tiers and tiers["t2"]["model"]
    assert "t3" in tiers and tiers["t3"]["model"]


def test_discover_all_models_probes_providers():
    with patch("argus.providers.discovery.is_antigravity_cli_available", return_value=True):
        with patch(
            "argus.providers.antigravity.AntigravityHostAdapter.discover_capabilities",
            return_value={"models": ["test-model-1", "test-model-2"]},
        ):
            models = discover_all_models(force_refresh=True)
            model_ids = [m.model_id for m in models]
            assert "test-model-1" in model_ids
            assert "test-model-2" in model_ids


def test_policy_modal_initializes_with_dynamic_tiers():
    cfg = ArgusConfig()
    modal = PolicyModal(current_config=cfg)
    assert hasattr(modal, "t1_model")
    assert hasattr(modal, "t2_model")
    assert hasattr(modal, "t3_model")
    assert modal.t1_model
    assert modal.t2_model
    assert modal.t3_model


def test_config_modal_initializes_with_discovered_models():
    cfg = ArgusConfig()
    modal = ConfigModal(config_type="model", current_config=cfg)
    assert modal.config_type == "model"


def _mixed_host_and_groq_models():
    return [
        DiscoveredModel(
            id="gemini-3.8-flash-medium",
            name="Gemini 3.8 Flash",
            provider="antigravity",
            tier="fast",
            is_available=True,
            status_label="Ready",
            status_tag="ready_host",
            description="Fast",
        ),
        DiscoveredModel(
            id="claude-sonnet-4-6",
            name="Claude Sonnet 4.6",
            provider="antigravity",
            tier="strong",
            is_available=True,
            status_label="Ready",
            status_tag="ready_host",
            description="Strong",
        ),
        DiscoveredModel(
            id="claude-opus-4-6-thinking",
            name="Claude Opus 4.6",
            provider="antigravity",
            tier="frontier",
            is_available=True,
            status_label="Ready",
            status_tag="ready_host",
            description="Frontier",
        ),
        DiscoveredModel(
            id="llama-3.3-70b-versatile",
            name="Llama 3.3 70B",
            provider="groq",
            tier="strong",
            is_available=True,
            status_label="Ready",
            status_tag="ready_key",
            description="Groq Strong",
        ),
        DiscoveredModel(
            id="qwen-2.5-coder-32b",
            name="Qwen 2.5 Coder 32B",
            provider="groq",
            tier="strong",
            is_available=True,
            status_label="Ready",
            status_tag="ready_key",
            description="Groq Code",
        ),
        DiscoveredModel(
            id="llama-3.1-8b-instant",
            name="Llama 3.1 8B",
            provider="groq",
            tier="fast",
            is_available=True,
            status_label="Ready",
            status_tag="ready_key",
            description="Groq Fast",
        ),
    ]


def test_resolve_dynamic_tiers_scopes_to_selected_model_and_provider():
    models = _mixed_host_and_groq_models()
    tiers = resolve_dynamic_tiers(
        models,
        active_provider="groq",
        selected_model="qwen-2.5-coder-32b",
    )
    assert tiers["t1"]["model"] == "qwen-2.5-coder-32b"
    assert tiers["t2"]["model"] == "qwen-2.5-coder-32b"
    assert tiers["t3"]["model"] == "qwen-2.5-coder-32b"


def test_resolve_dynamic_tiers_fast_model_escalates_within_same_provider():
    models = _mixed_host_and_groq_models()
    tiers = resolve_dynamic_tiers(
        models,
        active_provider="groq",
        selected_model="llama-3.1-8b-instant",
    )
    assert tiers["t1"]["model"] == "llama-3.1-8b-instant"
    assert tiers["t2"]["model"] == "llama-3.3-70b-versatile"
    assert tiers["t3"]["model"] == "llama-3.3-70b-versatile"


def test_resolve_dynamic_tiers_custom_model_not_hijacked_by_host():
    models = _mixed_host_and_groq_models()
    tiers = resolve_dynamic_tiers(
        models,
        active_provider="groq",
        selected_model="openai/gpt-oss-20b",
    )
    assert tiers["t1"]["model"] == "openai/gpt-oss-20b"
    assert tiers["t2"]["model"] == "openai/gpt-oss-20b"
    assert tiers["t3"]["model"] == "openai/gpt-oss-20b"


def test_policy_modal_respects_selected_provider_and_model():
    cfg = ArgusConfig(provider="groq", model="qwen-2.5-coder-32b")
    with patch("argus.providers.discovery._cached_discovery", _mixed_host_and_groq_models()):
        modal = PolicyModal(current_config=cfg)
        assert modal.t1_model == "qwen-2.5-coder-32b"
        assert modal.t2_model == "qwen-2.5-coder-32b"
        assert modal.t3_model == "qwen-2.5-coder-32b"

