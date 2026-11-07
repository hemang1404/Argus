"""Data models and presets for Argus LLM providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class ProviderConfigurationError(RuntimeError):
    """Typed configuration failure raised before any provider request is made."""

    code = "CONFIG_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class ProviderPreset:
    """Pre-configured definition for a known model provider."""
    key: str
    display_name: str
    default_base_url: str
    default_model: str
    requires_key: bool
    description: str
    extra_headers: Dict[str, str] = field(default_factory=dict)


PROVIDER_PRESETS: Dict[str, ProviderPreset] = {
    "antigravity": ProviderPreset(
        key="antigravity",
        display_name="Antigravity (Host Native)",
        default_base_url="host://antigravity-cli",
        default_model="gemini-2.5-flash",
        requires_key=False,
        description="Native host inference and adaptive reasoning effort via the Antigravity CLI without separate API keys.",
    ),
    "ollama": ProviderPreset(
        key="ollama",
        display_name="Ollama (Local Endpoint)",
        default_base_url="http://localhost:11434/v1",
        default_model="qwen2.5-coder:7b",
        requires_key=False,
        description="Locally hosted open-weight models via Ollama's OpenAI-compatible endpoint.",
    ),
    "openai": ProviderPreset(
        key="openai",
        display_name="OpenAI (Official API)",
        default_base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        requires_key=True,
        description="OpenAI flagship and lightweight models (GPT-4o, GPT-4o-mini).",
    ),
    "deepseek": ProviderPreset(
        key="deepseek",
        display_name="DeepSeek (Official API)",
        default_base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
        requires_key=True,
        description="DeepSeek-V3 and DeepSeek-R1 reasoning models.",
    ),
    "openrouter": ProviderPreset(
        key="openrouter",
        display_name="OpenRouter (Unified Router)",
        default_base_url="https://openrouter.ai/api/v1",
        default_model="anthropic/claude-3.5-sonnet",
        requires_key=True,
        description="Universal routing to Claude, OpenAI, Gemini, Llama, and open models.",
        extra_headers={"HTTP-Referer": "https://argus.dev", "X-Title": "Argus Code Recovery"},
    ),
    "gemini": ProviderPreset(
        key="gemini",
        display_name="Google AI Studio (Gemini API)",
        default_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        default_model="gemini-2.5-flash",
        requires_key=True,
        description="Google AI Studio Gemini API via official OpenAI-compatible endpoint.",
    ),
    "lmstudio": ProviderPreset(
        key="lmstudio",
        display_name="LM Studio (Local Endpoint)",
        default_base_url="http://localhost:1234/v1",
        default_model="local-model",
        requires_key=False,
        description="Locally loaded models hosted in LM Studio with OpenAI compatibility.",
    ),
    "custom": ProviderPreset(
        key="custom",
        display_name="Custom OpenAI-Compatible Endpoint",
        default_base_url="http://localhost:8000/v1",
        default_model="custom-model",
        requires_key=False,
        description="Any custom OpenAI-compatible server (vLLM, LocalAI, TGI, FastChat).",
    ),
}


@dataclass
class ProviderConfig:
    """Configured instance of an active or stored provider."""
    name: str
    base_url: str
    model: str
    api_key: Optional[str] = None
    temperature: float = 0.0
    max_tokens: int = 4096
    max_completion_tokens: Optional[int] = None
    reasoning_effort: Optional[str] = None
    timeout_seconds: float = 60.0
    extra_headers: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        pass

    @property
    def masked_key(self) -> str:
        """Return safely masked API key for display in logs and UI."""
        if not self.api_key:
            return "(none)"
        if len(self.api_key) <= 8:
            return "********"
        return f"{self.api_key[:4]}...{self.api_key[-4:]}"

    def to_dict(self, include_key: bool = False) -> Dict[str, Any]:
        """Serialize configuration to dictionary."""
        d: Dict[str, Any] = {
            "name": self.name,
            "base_url": self.base_url.rstrip("/"),
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout_seconds": self.timeout_seconds,
            "extra_headers": dict(self.extra_headers),
        }
        if include_key and self.api_key:
            d["api_key"] = self.api_key
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProviderConfig:
        """Create ProviderConfig from dictionary."""
        name = data.get("name", "custom")
        base_url = data.get("base_url", "https://api.openai.com/v1").rstrip("/")
        return cls(
            name=name,
            base_url=base_url,
            model=data.get("model", "gpt-4o-mini"),
            api_key=data.get("api_key"),
            temperature=float(data.get("temperature", 0.0)),
            max_tokens=int(data.get("max_tokens", 4096)),
            timeout_seconds=float(data.get("timeout_seconds", 60.0)),
            extra_headers=dict(data.get("extra_headers", {})),
        )

    @classmethod
    def from_preset(cls, preset_key: str, api_key: Optional[str] = None, model: Optional[str] = None) -> ProviderConfig:
        """Create a default configuration from a known preset."""
        key_lower = preset_key.lower().strip()
        if key_lower == "groq":
            raise ValueError(
                "Groq is strictly prohibited across Argus. "
                "Please configure an authorized provider (e.g. ollama, openai, deepseek, antigravity)."
            )
        preset = PROVIDER_PRESETS.get(key_lower)
        if not preset:
            # Unknown presets must never silently become a different provider.
            raise ProviderConfigurationError(
                f"Unknown provider preset '{preset_key}'. Configure a supported preset "
                "or an explicit custom endpoint; no provider was selected."
            )
        name = preset.key

        return cls(
            name=name,
            base_url=preset.default_base_url,
            model=model or preset.default_model,
            api_key=api_key,
            extra_headers=dict(preset.extra_headers),
        )
