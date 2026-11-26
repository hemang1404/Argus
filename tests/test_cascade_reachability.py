"""The cheap-first model cascade is a library feature: it applies only when a config has a
provider name and no model. The CLI and the MCP server always build their config from a stored
provider, and a stored provider always carries a model (the preset default when the user named
none), so those entry points pin one model for every attempt. These tests lock that behaviour in so
the documentation cannot drift from it again. If a future change makes the CLI or MCP leave the
model unset (so the cascade becomes reachable), update the docs that describe model dispatch
(README, reference/configuration, interfaces/python-api, guides/provider-configuration) too.
"""

from __future__ import annotations

import pytest

from argus.config import CASCADE_LADDERS, DEFAULT_BUDGET_USD, ArgusConfig
from argus.providers.models import ProviderConfig

LADDER_PRESETS = sorted(CASCADE_LADDERS)  # openai, deepseek, gemini, antigravity


def _tiers(cfg: ArgusConfig):
    return [cfg.model_for_tier(t) for t in (1, 2, 3)]


def test_library_config_with_provider_name_and_no_model_uses_the_cascade():
    cfg = ArgusConfig(provider="openai")
    small, large = CASCADE_LADDERS["openai"]
    assert cfg.ladder_source == "provider-cascade"
    assert _tiers(cfg) == [small, small, large]


@pytest.mark.parametrize("preset", LADDER_PRESETS)
def test_stored_provider_always_carries_a_model(preset):
    p_cfg = ProviderConfig.from_preset(preset)
    assert p_cfg.model  # filled from the preset default, never empty


@pytest.mark.parametrize("preset", LADDER_PRESETS)
def test_mcp_style_config_pins_the_stored_model(preset):
    # argus/mcp/server.py builds ArgusConfig(provider_config=<stored provider>) and nothing else.
    p_cfg = ProviderConfig.from_preset(preset)
    cfg = ArgusConfig(max_attempts=3, budget_limit_usd=DEFAULT_BUDGET_USD, provider_config=p_cfg)
    assert cfg.ladder_source == "pinned-model"
    assert _tiers(cfg) == [p_cfg.model] * 3


@pytest.mark.parametrize("preset", LADDER_PRESETS)
def test_cli_style_config_pins_the_stored_model(preset):
    # argus/cli.py builds ArgusConfig(provider=p_cfg.name, model=p_cfg.model, provider_config=p_cfg, ...).
    p_cfg = ProviderConfig.from_preset(preset)
    cfg = ArgusConfig(provider=p_cfg.name, model=p_cfg.model, provider_config=p_cfg, max_attempts=3, budget_limit_usd=DEFAULT_BUDGET_USD)
    assert cfg.ladder_source == "pinned-model"
    assert _tiers(cfg) == [p_cfg.model] * 3


def test_cli_model_flag_overrides_the_stored_model_for_every_attempt():
    from argus.cli import _resolve_provider

    class _Storage:
        def get_provider(self, name):
            return ProviderConfig.from_preset(name)

        def get_active_provider(self):
            return ProviderConfig.from_preset("openai")

    p_cfg = _resolve_provider("openai", "gpt-4o", _Storage(), "run")
    cfg = ArgusConfig(provider=p_cfg.name, model=p_cfg.model, provider_config=p_cfg)
    assert _tiers(cfg) == ["gpt-4o"] * 3
    assert cfg.ladder_source == "pinned-model"


def test_cascade_is_not_cheaper_default_for_deepseek_cli():
    # For deepseek the stored default (deepseek-chat) is the LARGER rung of the library ladder,
    # so a CLI run never uses the cheap first rung either.
    small, large = CASCADE_LADDERS["deepseek"]
    p_cfg = ProviderConfig.from_preset("deepseek")
    cfg = ArgusConfig(provider=p_cfg.name, model=p_cfg.model, provider_config=p_cfg)
    assert _tiers(cfg) == [p_cfg.model] * 3
    assert p_cfg.model == large and p_cfg.model != small
