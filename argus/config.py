"""Argus configuration and model pricing registry."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from argus.providers.storage import maybe_load_dotenv

# Conditionally load .env only if explicitly enabled via ARGUS_LOAD_DOTENV=1 or ARGUS_DOTENV_PATH
maybe_load_dotenv()

# Single source of truth for recovery-loop defaults and bounds. Surfaces (CLI, MCP,
# API, TUI) must import these instead of re-encoding their own numbers.
DEFAULT_MAX_ATTEMPTS = 3
MAX_ATTEMPTS_CEILING = 10
DEFAULT_BUDGET_USD = 0.030


class UnpricedModelError(ValueError):
    """Raised when a model has no known price and none was supplied explicitly.

    Argus refuses to guess a price: an under-priced model silently defeats the
    per-task budget guard.
    """


@dataclass(frozen=True)
class ModelCost:
    """Token pricing in USD per 1 Million tokens with cached input discount."""
    input_cost_per_m: float
    output_cost_per_m: float
    cached_input_cost_per_m: float = 0.0

    def calculate_cost(self, prompt_tokens: int, completion_tokens: int, cached_prompt_tokens: int = 0) -> float:
        """Calculate total USD cost for a given token usage.
        
        Strictly divides token counts by 1,000,000.0. Negative or non-integer usage
        (a buggy or hostile endpoint) is rejected rather than turned into a credit.
        """
        for name, value in (
            ("prompt_tokens", prompt_tokens),
            ("completion_tokens", completion_tokens),
            ("cached_prompt_tokens", cached_prompt_tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Invalid token usage: {name}={value!r} must be a non-negative integer.")
        cached_prompt_tokens = min(cached_prompt_tokens, prompt_tokens)
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


# Cheap-first model cascade, used ONLY when the caller did not name a model and the
# provider is listed here: attempts 1-2 use the small model (with failure feedback),
# attempt 3+ uses the large one. Any provider not listed gets no cascade and no
# model substitution. A caller that names a model (or an explicit small/large model)
# always overrides this table.
CASCADE_LADDERS: Dict[str, tuple[str, str]] = {
    "openai": ("gpt-4o-mini", "gpt-4o"),
    "deepseek": ("deepseek-flash", "deepseek-chat"),
    "gemini": ("gemini-2.5-flash", "gemini-2.5-pro"),
    "antigravity": ("gemini-3.8-flash-low", "gemini-3.8-flash-high"),
}

# Gateway-style pseudo-models that mean "no model chosen" rather than a real model id.
_PSEUDO_MODELS = {"argus/auto", "auto"}

# Providers whose inference does not bill per token at an Argus-visible price.
_FREE_PROVIDERS = {"ollama", "lmstudio", "antigravity"}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


@dataclass
class ArgusConfig:
    """Runtime configuration for Argus recovery sessions.

    Precedence for limits and model choice (highest first):
      1. Values passed by the caller (CLI flags, MCP/API arguments).
      2. Project policy file (``argus.yaml`` / ``.argus.yaml`` / ``pyproject.toml``
         ``[tool.argus]`` in the *workspace root*), applied by ``apply_project_policy``.
         The repository being analysed is untrusted input, so it may only TIGHTEN
         caps (lower ``max_attempts`` / ``budget_limit_usd`` / ``test_timeout_seconds``,
         disable escalation, set ``reasoning_effort`` when the caller left it unset).
         It can never raise a cap and never selects a model or provider.
      3. Built-in defaults (``DEFAULT_*`` constants above).
    Environment variables are consulted only for provider selection (``ARGUS_PROVIDER``).

    Model dispatch: ``model`` is authoritative. Only an explicit ``small_model`` /
    ``large_model`` ladder, or the documented provider cascade (``CASCADE_LADDERS``,
    only when no model was named), ever sends a different model. ``ladder_source``
    records which rule produced the models.
    """
    # Active provider and model (None by default on fresh install until configured)
    provider: Optional[str] = None
    model: Optional[str] = None
    deepseek_api_key: Optional[str] = field(default=None, repr=False)

    # Adaptive reasoning effort configuration
    effort: Optional[str] = None
    adaptive_routing: bool = True

    # Escalation ladder configuration (see class docstring)
    escalation_enabled: bool = True
    small_model: Optional[str] = None
    large_model: Optional[str] = None
    ladder_source: str = ""

    # User-supplied prices (USD per 1M tokens) for models absent from MODEL_REGISTRY.
    model_prices: Dict[str, ModelCost] = field(default_factory=dict)

    # Explicit provider configuration (if using Argus provider subsystem)
    provider_config: Optional[Any] = None

    # Recovery Loop Bounds
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
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
    budget_limit_usd: float = DEFAULT_BUDGET_USD

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

    # Tier 3 (token-equal tolerant match) is safe to leave on; this switch turns it off.
    enable_tier3: bool = True

    # Workspace snapshot options (see argus.runtime.workspace.WorkspaceManager).
    # snapshot_exclude: extra root-level directory/file names to leave out of the copy.
    # snapshot_max_bytes: fail fast above this size (None disables the limit).
    snapshot_exclude: List[str] = field(default_factory=list)
    snapshot_respect_gitignore: bool = True
    snapshot_max_bytes: Optional[int] = 2 * 1024 ** 3

    # UI and Terminal Branding Renderer (auto, braille, halfblock, sixel, kitty, text, box)
    logo_renderer: str = "auto"
    logo_layout: str = "wordmark"  # 'wordmark' (pure centered), 'inline' (A-in-eye on left), 'stacked'

    def __post_init__(self) -> None:
        p_name = (self.provider or (self.provider_config.name if self.provider_config else "")).strip().lower()
        if p_name == "groq":
            raise ValueError("Groq is strictly prohibited as an inference provider in Argus.")

        if self.model is not None:
            self.model = self.model.strip() or None
        if self.model is not None and self.model.lower() in _PSEUDO_MODELS:
            self.model = None  # gateway alias, not a real model id: use the provider's own default

        if self.provider_config is not None:
            self.provider = self.provider_config.name
            # An explicitly named model wins over the stored provider's default model.
            self.model = self.model or self.provider_config.model
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

        self._resolve_model_ladder()

    def _resolve_model_ladder(self) -> None:
        """Decide which model each attempt tier dispatches; never substitute silently."""
        p_norm = (self.provider or "").strip().lower()

        if self.small_model or self.large_model:
            # Caller supplied the ladder explicitly; a missing rung falls back to the
            # pinned model (or the other rung), never to a provider default.
            fallback = self.model or self.small_model or self.large_model
            self.small_model = self.small_model or fallback
            self.large_model = self.large_model or fallback
            self.model = self.model or self.small_model
            self.ladder_source = "explicit-ladder"
            return

        if self.model:
            self.small_model = self.large_model = self.model
            self.ladder_source = "pinned-model"
            return

        if self.escalation_enabled and p_norm in CASCADE_LADDERS:
            self.small_model, self.large_model = CASCADE_LADDERS[p_norm]
            self.model = self.small_model
            self.ladder_source = "provider-cascade"
            return

        from argus.providers.models import PROVIDER_PRESETS
        preset = PROVIDER_PRESETS.get(p_norm)
        self.model = preset.default_model if preset is not None else None
        self.small_model = self.large_model = self.model
        self.ladder_source = "provider-default" if self.model else "unresolved"

    def model_for_tier(self, tier_idx: int) -> Optional[str]:
        """Model dispatched at escalation tier ``tier_idx`` (1-2: small rung, 3: large rung)."""
        if not self.escalation_enabled or self.ladder_source not in ("explicit-ladder", "provider-cascade"):
            return self.model  # single model: read live so later `config.model = ...` is honoured
        return (self.small_model if tier_idx < 3 else self.large_model) or self.model

    def ladder_models(self) -> List[str]:
        """Distinct models this configuration may dispatch, in tier order."""
        models = [self.model_for_tier(t) for t in (1, 2, 3)]
        return list(dict.fromkeys(m for m in models if m))

    def apply_project_policy(self, workspace_root: Path) -> List[str]:
        """Apply the workspace's declarative policy, tighten-only. Returns notes/warnings.

        Reads ``argus.yaml``, ``argus.yml``, ``.argus.yaml``, ``.argus.yml`` or
        ``pyproject.toml`` ``[tool.argus]`` from ``workspace_root`` (never the process
        cwd). Parse errors and invalid values are returned as warnings, not swallowed.
        """
        policy, source, notes = _read_project_policy(Path(workspace_root))
        if not policy:
            return notes

        def tighten(key: str, cast, current):
            if key not in policy:
                return current
            try:
                value = cast(policy[key])
            except (TypeError, ValueError):
                notes.append(f"Ignored {key} in {source}: not a valid {cast.__name__}.")
                return current
            if not math.isfinite(value) or value <= 0:
                notes.append(f"Ignored {key} in {source}: value must be finite and positive.")
                return current
            if value < current:
                notes.append(f"Project policy ({source}) tightened {key}: {current} -> {value}.")
                return value
            if value > current:
                notes.append(
                    f"Ignored {key}={value} in {source}: a project file cannot raise a limit "
                    f"above the caller's value ({current})."
                )
            return current

        self.max_attempts = tighten("max_attempts", int, self.max_attempts)
        self.budget_limit_usd = tighten("budget_limit_usd", float, self.budget_limit_usd)
        self.test_timeout_seconds = tighten("test_timeout_seconds", int, self.test_timeout_seconds)
        if policy.get("escalation_enabled") is False and self.escalation_enabled:
            self.escalation_enabled = False
            notes.append(f"Project policy ({source}) disabled escalation.")
        if "reasoning_effort" in policy and not self.reasoning_effort:
            self.reasoning_effort = str(policy["reasoning_effort"])
            notes.append(f"Project policy ({source}) set reasoning_effort={self.reasoning_effort}.")
        for key in ("model", "small_model", "large_model", "provider"):
            if key in policy:
                notes.append(
                    f"Ignored {key} in {source}: a project file cannot select the model or provider; "
                    "use --provider/--model."
                )
        return notes

    def _is_free_provider(self) -> bool:
        """True when inference is host-native or served from this machine, so an unpriced
        model has no Argus-visible price. Remote endpoints are never assumed free."""
        name = (self.provider or "").strip().lower()
        if name in _FREE_PROVIDERS:
            return True
        base_url = getattr(self.provider_config, "base_url", "") if self.provider_config is not None else ""
        if not base_url and name == "custom":
            base_url = "http://localhost"  # the 'custom' preset defaults to a local endpoint
        if not base_url:
            return False
        from urllib.parse import urlparse
        return (urlparse(base_url).hostname or "").lower() in _LOCAL_HOSTS

    def get_model_cost(self, model: Optional[str] = None) -> ModelCost:
        """Look up the price of ``model`` (default: the configured model).

        Fails closed: a model with no registry or user-supplied price raises
        ``UnpricedModelError`` unless the provider is local/host-native (free).
        """
        target_model = model or self.model
        if not target_model:
            raise UnpricedModelError(
                "No model is configured, so the cost cannot be bounded. "
                "Pass --model or configure a provider model."
            )
        if target_model in self.model_prices:
            return self.model_prices[target_model]
        if target_model in MODEL_REGISTRY:
            return MODEL_REGISTRY[target_model]
        if self._is_free_provider():
            return ModelCost(input_cost_per_m=0.0, output_cost_per_m=0.0)
        raise UnpricedModelError(
            f"Model '{target_model}' has no known price for provider '{self.provider}'. "
            "Argus will not guess a price; add it to ArgusConfig.model_prices "
            "(USD per 1M tokens) or use a priced model."
        )

    def validate(self, check_provider: bool = True) -> List[str]:
        """Check configuration health and return any missing requirements.

        ``check_provider=False`` skips provider/credential checks (an adapter was injected).
        Pricing is always checked: every model the ladder may dispatch must be priced.
        """
        issues: List[str] = []

        if not check_provider:
            pass
        elif self.provider_config is not None:
            from argus.providers.models import PROVIDER_PRESETS
            preset = PROVIDER_PRESETS.get(self.provider_config.name.lower())
            if preset and preset.requires_key and not self.provider_config.api_key:
                issues.append(f"API key is not configured for provider '{self.provider_config.name}'. Set it via 'argus provider set' or environment variable.")
        elif self.provider:
            from argus.providers.models import PROVIDER_PRESETS
            p_key = self.provider.strip().lower()
            if p_key not in PROVIDER_PRESETS and p_key != "auto":
                issues.append(
                    f"Unknown provider '{self.provider}'. Known providers: "
                    f"{', '.join(sorted(PROVIDER_PRESETS))}, or 'auto'. Configure one with 'argus provider set'."
                )
            elif self.provider == "deepseek" and not self.deepseek_api_key and not os.getenv("DEEPSEEK_API_KEY"):
                issues.append("DEEPSEEK_API_KEY is not set in environment or OS credential store.")
            elif self.provider == "openai" and not os.getenv("OPENAI_API_KEY"):
                from argus.providers.storage import ProviderStorage
                if not ProviderStorage().get_api_key("openai"):
                    issues.append("OPENAI_API_KEY is not configured.")
            elif self.provider == "gemini" and not os.getenv("GEMINI_API_KEY") and not os.getenv("GOOGLE_API_KEY"):
                from argus.providers.storage import ProviderStorage
                if not ProviderStorage().get_api_key("gemini") and not ProviderStorage().get_api_key("google"):
                    issues.append("GEMINI_API_KEY / GOOGLE_API_KEY is not configured.")
            # Antigravity uses host-native authentication and does not require an API key
        else:
            issues.append("No active LLM provider configured. Run 'argus provider set' to configure a provider.")

        models = self.ladder_models()
        if not models:
            issues.append("No model is configured, so cost cannot be bounded. Pass --model or configure a provider model.")
        for model_name in models:
            try:
                self.get_model_cost(model_name)
            except UnpricedModelError as ex:
                issues.append(str(ex))

        if (
            not isinstance(self.max_attempts, int)
            or isinstance(self.max_attempts, bool)
            or not 1 <= self.max_attempts <= MAX_ATTEMPTS_CEILING
        ):
            issues.append(f"max_attempts must be an integer between 1 and {MAX_ATTEMPTS_CEILING}.")
        if not _is_positive_finite(self.budget_limit_usd):
            issues.append("budget_limit_usd must be a finite number greater than 0.")
        for name in ("task_deadline_seconds", "provider_timeout_seconds", "rate_limit_backoff_max_seconds"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
                issues.append(f"{name} must be a finite non-negative number.")
        return issues


def _is_positive_finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _read_project_policy(root: Path) -> tuple[dict, str, List[str]]:
    """Return (policy dict, source file name, warnings) for the workspace root."""
    warnings: List[str] = []
    for name in ("argus.yaml", "argus.yml", ".argus.yaml", ".argus.yml"):
        path = root / name
        if not path.is_file():
            continue
        try:
            import yaml
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as ex:  # parse/IO errors are reported, never swallowed
            warnings.append(f"Could not read project policy {name}: {type(ex).__name__}: {ex}")
            continue
        if isinstance(data, dict):
            return data, name, warnings
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except Exception as ex:
            warnings.append(f"Could not parse pyproject.toml for [tool.argus]: {type(ex).__name__}: {ex}")
            return {}, "", warnings
        section = data.get("tool", {}).get("argus", {})
        if isinstance(section, dict) and section:
            return section, "pyproject.toml", warnings
    return {}, "", warnings
