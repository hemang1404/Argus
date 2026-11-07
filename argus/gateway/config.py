"""Gateway configuration loader supporting YAML files and environment defaults."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field, model_validator
import yaml
from argus.providers.storage import maybe_load_dotenv

maybe_load_dotenv()


class ProviderConfig(BaseModel):
    """Upstream inference provider configuration."""
    name: str
    kind: Literal["openai_compatible", "anthropic"] = "openai_compatible"
    base_url: str
    key_env: str = ""
    api_key: Optional[str] = None

    @model_validator(mode="after")
    def reject_groq(self) -> ProviderConfig:
        if self.name.lower() == "groq" or "groq.com" in self.base_url.lower():
            raise ValueError(
                "Groq is strictly prohibited across all of Argus. "
                "The developer's Groq key is private and cannot be used for inference, "
                "recovery, fallback, or benchmarks."
            )
        return self

    def get_api_key(self) -> str:
        """Resolve API key from direct value or environment variable."""
        if self.api_key:
            return self.api_key
        if self.key_env:
            return os.getenv(self.key_env, "")
        return ""


class TierDefinition(BaseModel):
    """Model tier definition mapping to a provider and upstream model."""
    provider: str
    model: str


class BudgetConfig(BaseModel):
    """Budget guardrails for controlling inference expenditure."""
    per_task_usd: float = 0.05
    per_session_usd: float = 1.00
    on_exhausted: Literal["fail_fast", "warn"] = "fail_fast"


class HealingConfig(BaseModel):
    """In-memory streaming patch healing configuration."""
    enabled: bool = True
    workspace: str = "."
    similarity_threshold: float = 0.82
    ambiguity_margin: float = 0.10


class GatewayConfig(BaseModel):
    """Master configuration for the Argus local proxy gateway."""
    host: str = "127.0.0.1"
    port: int = 8000
    api_key: Optional[str] = None
    allow_remote: bool = False
    rate_limit_per_minute: int = 120
    providers: List[ProviderConfig] = Field(default_factory=list)
    tiers: Dict[str, TierDefinition] = Field(default_factory=dict)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    healing: HealingConfig = Field(default_factory=HealingConfig)

    def get_gateway_key(self) -> Optional[str]:
        """Resolve expected gateway API key from config or environment."""
        return self.api_key or os.getenv("ARGUS_GATEWAY_API_KEY")

    @classmethod
    def load(cls, config_path: Optional[str | Path] = None) -> GatewayConfig:
        """Load configuration from a YAML file or construct default from environment."""
        p = Path(config_path) if config_path else Path("argus.yaml")
        if p.exists() and p.is_file():
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                return cls(**data)
            except Exception as ex:
                print(f"[Argus Warning] Failed to parse {p}: {ex}. Falling back to default environment config.")

        # Discover available providers from user storage, host runtime, and environment
        providers = []
        tiers = {}

        # 1. User-configured providers from ProviderStorage
        try:
            from argus.providers.storage import ProviderStorage
            storage = ProviderStorage()
            for p_item in storage.list_providers():
                if p_item.api_key or not getattr(p_item, "requires_key", True) or p_item.name == "antigravity":
                    providers.append(
                        ProviderConfig(
                            name=p_item.name,
                            kind="openai_compatible",
                            base_url=p_item.base_url,
                            key_env=f"{p_item.name.upper()}_API_KEY",
                            api_key=p_item.api_key,
                        )
                    )
                    if "cheap" not in tiers:
                        tiers["cheap"] = TierDefinition(provider=p_item.name, model=p_item.model)
                    if "strong" not in tiers:
                        tiers["strong"] = TierDefinition(provider=p_item.name, model=p_item.model)
                    if "frontier" not in tiers:
                        tiers["frontier"] = TierDefinition(provider=p_item.name, model=p_item.model)
        except Exception:
            pass

        # 2. Host IDE runtime (like Antigravity subscription)
        try:
            from argus.providers.antigravity import find_antigravity_cli
            if find_antigravity_cli() is not None and not any(p.name == "antigravity" for p in providers):
                providers.append(
                    ProviderConfig(
                        name="antigravity",
                        kind="openai_compatible",
                        base_url="host://antigravity-cli",
                    )
                )
                if "cheap" not in tiers:
                    tiers["cheap"] = TierDefinition(provider="antigravity", model="gemini-2.5-flash")
                if "strong" not in tiers:
                    tiers["strong"] = TierDefinition(provider="antigravity", model="gemini-2.5-pro")
                if "frontier" not in tiers:
                    tiers["frontier"] = TierDefinition(provider="antigravity", model="gemini-2.5-pro")
        except Exception:
            pass

        # No environment-variable-only providers. A credential being present is
        # not a request to use that provider; Gateway providers must be configured
        # explicitly (or supplied by the stored active provider above).

        # Ensure minimal default tiers exist from available providers
        if "cheap" not in tiers and providers:
            tiers["cheap"] = TierDefinition(provider=providers[0].name, model="default")
        if "strong" not in tiers and "cheap" in tiers:
            tiers["strong"] = tiers["cheap"]
        if "frontier" not in tiers and "strong" in tiers:
            tiers["frontier"] = tiers["strong"]

        return cls(providers=providers, tiers=tiers)
