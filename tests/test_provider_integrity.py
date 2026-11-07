"""WP-4 provider-integrity regression tests (mocked, no network)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from argus.config import ArgusConfig
from argus.runtime import controller as controller_module
from argus.providers.models import (
    PROVIDER_PRESETS,
    ProviderConfig,
    ProviderConfigurationError,
)
from argus.providers.storage import ProviderStorage
from argus.runtime.controller import RecoveryController


class _ExplodingAdapter:
    def __init__(self, *args, **kwargs):
        raise AssertionError("OpenAICompatibleAdapter must not be constructed")


@pytest.fixture(autouse=True)
def clean_provider_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARGUS_CONFIG_DIR", str(tmp_path / "config"))
    for key in (
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "ARGUS_API_KEY",
        "ARGUS_PROVIDER",
        "GROQ_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_unknown_provider_with_deepseek_key_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-should-not-select-deepseek")
    cfg = ArgusConfig(provider="unknown-provider")
    assert cfg.provider == "unknown-provider"
    assert cfg.model is None

    monkeypatch.setattr(controller_module, "OpenAICompatibleAdapter", _ExplodingAdapter)
    with pytest.raises(ProviderConfigurationError) as exc:
        RecoveryController(config=cfg)._get_adapter()
    assert exc.value.code == "CONFIG_ERROR"


def test_unknown_provider_without_any_key_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = ArgusConfig(provider="unknown-provider")
    monkeypatch.setattr(controller_module, "OpenAICompatibleAdapter", _ExplodingAdapter)
    with pytest.raises(ProviderConfigurationError):
        RecoveryController(config=cfg)._get_adapter()


def test_deepseek_key_alone_does_not_select_a_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-present")
    cfg = ArgusConfig()
    assert cfg.provider is None
    assert cfg.model is None


def test_unknown_preset_is_rejected() -> None:
    assert "groq" not in PROVIDER_PRESETS
    with pytest.raises(ProviderConfigurationError):
        ProviderConfig.from_preset("not-a-real-preset")


def test_explicit_deepseek_selection_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-explicit")
    cfg = ArgusConfig(provider="deepseek")
    adapter = RecoveryController(config=cfg)._get_adapter()
    assert adapter.config.base_url == "https://api.deepseek.com/v1"
    assert adapter.config.name == "deepseek"


def test_explicit_custom_endpoint_still_works(clean_provider_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = ProviderStorage(config_dir=clean_provider_env / "config")
    storage.save_provider(
        ProviderConfig(
            name="custom",
            base_url="http://127.0.0.1:9876/v1",
            model="local-model",
            api_key="local-key",
        )
    )
    cfg = ArgusConfig(provider="custom")
    adapter = RecoveryController(config=cfg)._get_adapter()
    assert adapter.config.base_url == "http://127.0.0.1:9876/v1"


def test_explicit_antigravity_selection_still_works() -> None:
    cfg = ArgusConfig(provider="antigravity")
    adapter = RecoveryController(config=cfg)._get_adapter()
    assert adapter.config.name == "antigravity"


def test_missing_credentials_for_explicit_provider_does_not_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = ArgusConfig(provider="openai")
    monkeypatch.setattr(controller_module, "OpenAICompatibleAdapter", _ExplodingAdapter)
    with pytest.raises(RuntimeError) as exc:
        RecoveryController(config=cfg)._get_adapter()
    assert "No API key" in str(exc.value)


def test_groq_is_rejected_through_every_storage_path(clean_provider_env: Path) -> None:
    with pytest.raises(ValueError):
        ArgusConfig(provider="groq")
    storage = ProviderStorage(config_dir=clean_provider_env / "config")
    with pytest.raises(ValueError):
        storage.save_provider(ProviderConfig(name="groq", base_url="https://api.groq.com", model="m"))
    with pytest.raises(ValueError):
        storage.set_api_key("groq", "gsk-test")
    assert storage.get_provider("groq") is None
    assert storage.set_active_provider("groq") is False


def test_no_provider_and_no_host_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import argus.providers.antigravity as antigravity

    monkeypatch.setattr(antigravity, "find_antigravity_cli", lambda: None)
    cfg = ArgusConfig()
    with pytest.raises(ProviderConfigurationError):
        RecoveryController(config=cfg)._get_adapter()


def test_missing_provider_does_not_auto_discover_host(monkeypatch: pytest.MonkeyPatch) -> None:
    import argus.providers.antigravity as antigravity

    monkeypatch.setattr(antigravity, "find_antigravity_cli", lambda: Path("/tmp/fake-agy"))
    cfg = ArgusConfig()
    with pytest.raises(ProviderConfigurationError):
        RecoveryController(config=cfg)._get_adapter()


def test_explicit_auto_discovers_host_and_is_observable(monkeypatch: pytest.MonkeyPatch) -> None:
    import argus.providers.antigravity as antigravity

    monkeypatch.setattr(antigravity, "find_antigravity_cli", lambda: Path("/tmp/fake-agy"))
    cfg = ArgusConfig(provider="auto")
    controller = RecoveryController(config=cfg)
    adapter = controller._get_adapter()

    assert adapter.config.name == "antigravity"
    assert controller.selected_provider == "antigravity"


def test_explicit_auto_prefers_stored_active_provider(clean_provider_env: Path) -> None:
    storage = ProviderStorage(config_dir=clean_provider_env / "config")
    storage.save_provider(
        ProviderConfig(
            name="ollama",
            base_url="http://127.0.0.1:11434/v1",
            model="qwen",
            api_key="",
        )
    )
    assert storage.set_active_provider("ollama") is True

    cfg = ArgusConfig(provider="auto")
    controller = RecoveryController(config=cfg)
    adapter = controller._get_adapter()

    assert adapter.config.name == "ollama"
    assert controller.selected_provider == "ollama"


def test_explicit_auto_without_host_or_active_provider_is_config_error(
    clean_provider_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argus.providers.antigravity as antigravity

    monkeypatch.setattr(antigravity, "find_antigravity_cli", lambda: None)
    cfg = ArgusConfig(provider="auto")
    with pytest.raises(ProviderConfigurationError):
        RecoveryController(config=cfg)._get_adapter()
