"""Example: inspect provider presets and the model ladder Argus would use.

Offline: nothing here contacts a provider or reads your stored credentials.

Model selection rules (``argus.config.ArgusConfig``):
  * ``model=...`` pins one model. It is the only model sent; there is no escalation.
  * No model named, provider with a documented cheap-first cascade, escalation on:
    the cheaper model for attempts 1-2 and the stronger model for attempt 3.
  * Any other provider: the provider's own default model, no escalation.
A model with no known price is refused (``CONFIG_ERROR``) unless the provider is
local, or you supply a price via ``ArgusConfig.model_prices``.
"""

from __future__ import annotations

from argus.config import CASCADE_LADDERS, ArgusConfig
from argus.providers.models import PROVIDER_PRESETS


def main() -> None:
    print("Provider presets:")
    for key, preset in PROVIDER_PRESETS.items():
        needs_key = "key required" if preset.requires_key else "no key"
        print(f"  - {key}: default model {preset.default_model} ({needs_key})")

    print("\nCheap-first cascades (used only when no model is pinned):")
    for provider, (small, large) in CASCADE_LADDERS.items():
        print(f"  - {provider}: attempts 1-2 {small}, attempt 3 {large}")

    print("\nResolved ladders:")
    for label, cfg in (
        ("openai, nothing pinned", ArgusConfig(provider="openai")),
        ("openai, model pinned", ArgusConfig(provider="openai", model="gpt-4o")),
        ("ollama, nothing pinned", ArgusConfig(provider="ollama")),
    ):
        print(f"  {label}: source={cfg.ladder_source!r} models={cfg.ladder_models()}")


if __name__ == "__main__":
    main()
