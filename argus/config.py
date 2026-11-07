"""Argus configuration and model pricing registry."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, TYPE_CHECKING
from argus.providers.storage import maybe_load_dotenv

if TYPE_CHECKING:
    from argus.providers.models import ProviderConfig

# Conditionally load .env only if explicitly enabled via ARGUS_LOAD_DOTENV=1 or ARGUS_DOTENV_PATH
maybe_load_dotenv()


@dataclass(frozen=True)
class ModelCost:
    """Token pricing in USD per 1 Million tokens with cached input discount."""
    input_cost_per_m: float
    output_cost_per_m: float
    cached_input_cost_per_m: float = 0.0

    def calculate_cost(self, prompt_tokens: int, completion_tokens: int, cached_prompt_tokens: int = 0) -> float:
        """Calculate total USD cost for a given token usage.
        
        Strictly divides token counts by 1,000,000.0.
        """
        uncached_prompt = max(0, prompt_tokens - cached_prompt_tokens)
        input_usd = (uncached_prompt / 1_000_000.0) * self.input_cost_per_m
        cached_rate = self.cached_input_cost_per_m if self.cached_input_cost_per_m > 0 else self.input_cost_per_m
        cached_input_usd = (cached_prompt_tokens / 1_000_000.0) * cached_rate
        output_usd = (completion_tokens / 1_000_000.0) * self.output_cost_per_m
        return round(input_usd + cached_input_usd + output_usd, 7)


# Active Model Registry with token economics
MODEL_REGISTRY: Dict[str, ModelCost] = {
    # Groq OpenAI open-weight models (Official Groq API rates as of Oct 28, 2026)
    "openai/gpt-oss-20b": ModelCost(input_cost_per_m=0.075, output_cost_per_m=0.300, cached_input_cost_per_m=0.0375),
    "gpt-oss-20b": ModelCost(input_cost_per_m=0.075, output_cost_per_m=0.300, cached_input_cost_per_m=0.0375),
    "openai/gpt-oss-120b": ModelCost(input_cost_per_m=0.150, output_cost_per_m=0.600, cached_input_cost_per_m=0.0750),
    "gpt-oss-120b": ModelCost(input_cost_per_m=0.150, output_cost_per_m=0.600, cached_input_cost_per_m=0.0750),
    "llama-3.3-70b-versatile": ModelCost(input_cost_per_m=0.590, output_cost_per_m=0.790),
    "llama-3.1-8b-instant": ModelCost(input_cost_per_m=0.050, output_cost_per_m=0.080),
    "qwen-2.5-coder-32b": ModelCost(input_cost_per_m=0.790, output_cost_per_m=0.790),
    # Antigravity host-native models (subscription / host-authenticated)
    "gemini-3.8-flash-low": ModelCost(input_cost_per_m=0.0, output_cost_per_m=0.0),
    "gemini-3.8-flash-medium": ModelCost(input_cost_per_m=0.0, output_cost_per_m=0.0),
    "gemini-3.8-flash-high": ModelCost(input_cost_per_m=0.0, output_cost_per_m=0.0),
    "claude-sonnet-4-6": ModelCost(input_cost_per_m=0.0, output_cost_per_m=0.0),
    "claude-opus-4-6-thinking": ModelCost(input_cost_per_m=0.0, output_cost_per_m=0.0),
    "gpt-oss-120b-medium": ModelCost(input_cost_per_m=0.0, output_cost_per_m=0.0),
    "gemini-3.8-flash": ModelCost(input_cost_per_m=0.0, output_cost_per_m=0.0),
    "gemini-3.8-pro": ModelCost(input_cost_per_m=0.0, output_cost_per_m=0.0),
    "gemini-3-flash": ModelCost(input_cost_per_m=0.0, output_cost_per_m=0.0),
    "gemini-3-pro": ModelCost(input_cost_per_m=0.0, output_cost_per_m=0.0),
    "antigravity": ModelCost(input_cost_per_m=0.0, output_cost_per_m=0.0),

    # Google AI Studio / Gemini API standard pay-as-you-go rates (USD per 1M tokens)
    # Source: https://ai.google.dev/gemini-api/docs/pricing
    # Verification Date: November 7, 2026
    # Note on thinking tokens: Google AI Studio counts thinking tokens as output tokens billed at the output rate.
    # Note on context caching: Cached prompt reads receive an approx 75% discount off base input rates.
    # Prompts <= 200K tokens:
    #   - gemini-2.5-flash: $0.30 input / $2.50 output / $0.075 cached input
    #   - gemini-2.5-pro: $1.25 input / $10.00 output / $0.3125 cached input
    # Higher-context pricing (> 200K tokens):
    #   - gemini-2.5-flash (>200K): $0.60 input / $5.00 output / $0.15 cached input
    #   - gemini-2.5-pro (>200K): $2.50 input / $15.00 output / $0.625 cached input
    "gemini-2.5-flash": ModelCost(
        input_cost_per_m=0.300,
        output_cost_per_m=2.500,
        cached_input_cost_per_m=0.075,
    ),
    "gemini-2.5-pro": ModelCost(
        input_cost_per_m=1.250,
        output_cost_per_m=10.000,
        cached_input_cost_per_m=0.3125,
    ),
    "gemini-2.5-flash-lite": ModelCost(
        input_cost_per_m=0.075,
        output_cost_per_m=0.300,
        cached_input_cost_per_m=0.01875,
    ),
    # OpenAI models
    "gpt-4o-mini": ModelCost(input_cost_per_m=0.15, output_cost_per_m=0.60),
    "gpt-4o": ModelCost(input_cost_per_m=2.50, output_cost_per_m=10.00),
    # DeepSeek models
    "deepseek-flash": ModelCost(input_cost_per_m=0.14, output_cost_per_m=0.28),
    "deepseek-chat": ModelCost(input_cost_per_m=0.27, output_cost_per_m=1.10),
    # OpenRouter / Claude
    "anthropic/claude-3.5-sonnet": ModelCost(input_cost_per_m=3.00, output_cost_per_m=15.00),
    # Local open-weight models
    "qwen2.5-coder:7b": ModelCost(input_cost_per_m=0.0, output_cost_per_m=0.0),
    "local-model": ModelCost(input_cost_per_m=0.0, output_cost_per_m=0.0),
    "mock-model": ModelCost(input_cost_per_m=0.0, output_cost_per_m=0.0),
}


@dataclass
class ArgusConfig:
    """Runtime configuration for Argus recovery sessions."""
    # Active provider and model (None by default on fresh install until configured)
    provider: Optional[str] = None
    model: Optional[str] = None
    deepseek_api_key: Optional[str] = None
    
    # Adaptive reasoning effort configuration
    effort: Optional[str] = None
    adaptive_routing: bool = True

    # Multi-Tier Escalation Ladder Configuration
    escalation_enabled: bool = True
    small_model: Optional[str] = None
    large_model: Optional[str] = None

    # Explicit provider configuration (if using Argus provider subsystem)
    provider_config: Optional[Any] = None

    # Recovery Loop Bounds (at most 3 turns by default)
    max_attempts: int = 3
    test_timeout_seconds: int = 30
    repo_test_timeout_seconds: int = 180
    provider_timeout_seconds: float = 45.0
    rate_limit_backoff_max_seconds: float = 60.0
    allow_rate_limit_retries: bool = True
    task_deadline_seconds: float = 300.0
    max_tokens: int = 2500
    max_completion_tokens: Optional[int] = None
    reasoning_effort: Optional[str] = None
    temperature: float = 0.0
    include_feedback: bool = True
    budget_limit_usd: float = 0.030
    global_experiment_budget_usd: float = 5.000

    # Container evaluation harness parameters (Milestone 2)
    use_container_runner: bool = False
    require_container_isolation: bool = False
    container_image: Optional[str] = None
    base_commit: Optional[str] = None
    expected_candidate_hash: Optional[str] = None
    
    # Patch Engine Parameters
    similarity_threshold: float = 0.82
    ambiguity_margin: float = 0.10
    # Experimental: Tier 4 anchor recovery has narrow positive coverage and is
    # not enabled by default. Enable explicitly only after independent review.
    enable_tier4: bool = False

    # UI and Terminal Branding Renderer (auto, braille, halfblock, sixel, kitty, text, box)
    logo_renderer: str = "auto"
    logo_layout: str = "wordmark"  # 'wordmark' (pure centered), 'inline' (A-in-eye on left), 'stacked'

    def __post_init__(self) -> None:
        p_name = (self.provider or (self.provider_config.name if self.provider_config else "")).strip().lower()
        if p_name == "groq":
            raise ValueError("Groq is strictly prohibited as an inference provider in Argus.")

        # Load project-level declarative policy configuration (argus.yaml, .argus.yaml, or pyproject.toml [tool.argus])
        self._load_declarative_policy()

        if self.provider_config is not None:
            self.provider = self.provider_config.name
            self.model = self.provider_config.model
            if self.provider == "deepseek" and self.provider_config.api_key:
                self.deepseek_api_key = self.provider_config.api_key
        elif not self.provider:
            # Load active provider configuration from storage if available
            try:
                from argus.providers.storage import ProviderStorage
                storage = ProviderStorage()
                active = storage.get_active_provider()
                if active:
                    self.provider = active.name
                    self.model = self.model or active.model
                    if active.api_key and self.provider == "deepseek":
                        self.deepseek_api_key = active.api_key
                    self.provider_config = active
                elif self.provider is None:
                    env_provider = os.getenv("ARGUS_PROVIDER")
                    if env_provider:
                        self.provider = env_provider
                    # No API key may select a provider. DEEPSEEK_API_KEY (or any
                    # other credential) is only consulted after an explicit
                    # provider has been selected.

            except ValueError:
                raise
            except Exception:
                pass

        # Populate escalation tier model defaults if not explicitly set
        p_norm = (self.provider or "").strip().lower()
        if not self.small_model:
            if p_norm == "openai":
                self.small_model = "gpt-4o-mini"
            elif p_norm == "deepseek":
                self.small_model = "deepseek-flash"
            elif p_norm == "gemini":
                self.small_model = "gemini-2.5-flash"
            elif p_norm == "groq":
                self.small_model = "llama-3.1-8b-instant"
            elif p_norm == "antigravity":
                self.small_model = "gemini-2.5-flash"
            elif p_norm == "ollama":
                self.small_model = "qwen2.5-coder:7b"
            else:
                self.small_model = self.model or "qwen2.5-coder:7b"

        if not self.large_model:
            if p_norm == "openai":
                self.large_model = "gpt-4o"
            elif p_norm == "deepseek":
                self.large_model = "deepseek-chat"
            elif p_norm == "gemini":
                self.large_model = "gemini-2.5-pro"
            elif p_norm == "groq":
                self.large_model = "llama-3.3-70b-versatile"
            elif p_norm == "antigravity":
                self.large_model = "gemini-2.5-pro"
            elif p_norm == "ollama":
                self.large_model = "qwen2.5-coder:7b"
            else:
                self.large_model = self.model or "gpt-4o"

    def _load_declarative_policy(self) -> None:
        """Load declarative policy settings from local argus.yaml, .argus.yaml, or pyproject.toml."""
        cwd = Path.cwd()
        candidate_files = [
            cwd / "argus.yaml",
            cwd / "argus.yml",
            cwd / ".argus.yaml",
            cwd / ".argus.yml",
        ]
        policy_dict: dict = {}
        for p in candidate_files:
            if p.is_file():
                try:
                    import yaml
                    data = yaml.safe_load(p.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        policy_dict = data
                        break
                except Exception:
                    pass

        if not policy_dict and (cwd / "pyproject.toml").is_file():
            try:
                import tomli
                t_data = tomli.loads((cwd / "pyproject.toml").read_text(encoding="utf-8"))
                policy_dict = t_data.get("tool", {}).get("argus", {})
            except Exception:
                try:
                    import tomllib
                    t_data = tomllib.loads((cwd / "pyproject.toml").read_text(encoding="utf-8"))
                    policy_dict = t_data.get("tool", {}).get("argus", {})
                except Exception:
                    pass

        if policy_dict:
            if "max_attempts" in policy_dict:
                self.max_attempts = int(policy_dict["max_attempts"])
            if "budget_limit_usd" in policy_dict:
                self.budget_limit_usd = float(policy_dict["budget_limit_usd"])
            if "test_timeout_seconds" in policy_dict:
                self.test_timeout_seconds = int(policy_dict["test_timeout_seconds"])
            if "model" in policy_dict and not self.model:
                self.model = str(policy_dict["model"])
            if "small_model" in policy_dict and not self.small_model:
                self.small_model = str(policy_dict["small_model"])
            if "large_model" in policy_dict and not self.large_model:
                self.large_model = str(policy_dict["large_model"])
            if "escalation_enabled" in policy_dict:
                self.escalation_enabled = bool(policy_dict["escalation_enabled"])
            if "reasoning_effort" in policy_dict:
                self.reasoning_effort = str(policy_dict["reasoning_effort"])

    def get_model_cost(self, model: Optional[str] = None) -> ModelCost:
        """Lookup pricing for configured or specified model, fallback to conservative estimate."""
        target_model = model or self.model
        if not target_model:
            return ModelCost(input_cost_per_m=0.50, output_cost_per_m=0.50)
        return MODEL_REGISTRY.get(target_model, ModelCost(input_cost_per_m=0.50, output_cost_per_m=0.50))

    def validate(self) -> list[str]:
        """Check configuration health and return any missing requirements."""
        issues = []

        if self.provider_config is not None:
            from argus.providers.models import PROVIDER_PRESETS
            preset = PROVIDER_PRESETS.get(self.provider_config.name.lower())
            if preset and preset.requires_key and not self.provider_config.api_key:
                issues.append(f"API key is not configured for provider '{self.provider_config.name}'. Set it via 'argus provider set' or environment variable.")
        elif self.provider:
            if self.provider == "deepseek" and not self.deepseek_api_key and not os.getenv("DEEPSEEK_API_KEY"):
                issues.append("DEEPSEEK_API_KEY is not set in environment or OS credential store.")
            elif self.provider == "openai" and not os.getenv("OPENAI_API_KEY"):
                from argus.providers.storage import ProviderStorage
                if not ProviderStorage().get_api_key("openai"):
                    issues.append("OPENAI_API_KEY is not configured.")
            elif self.provider == "gemini" and not os.getenv("GEMINI_API_KEY") and not os.getenv("GOOGLE_API_KEY"):
                from argus.providers.storage import ProviderStorage
                if not ProviderStorage().get_api_key("gemini") and not ProviderStorage().get_api_key("google"):
                    issues.append("GEMINI_API_KEY / GOOGLE_API_KEY is not configured.")
            elif self.provider == "groq" and not os.getenv("GROQ_API_KEY"):
                from argus.providers.storage import ProviderStorage
                if not ProviderStorage().get_api_key("groq"):
                    issues.append("GROQ_API_KEY is not configured.")
            elif self.provider == "antigravity":
                # Antigravity uses host-native authentication and does not require an API key
                pass
        else:
            issues.append("No active LLM provider configured. Run 'argus provider set' to configure a provider.")

        if self.max_attempts < 1 or self.max_attempts > 10:
            issues.append("max_attempts must be between 1 and 10.")
        if self.budget_limit_usd <= 0:
            issues.append("budget_limit_usd must be greater than 0.")
        return issues
