"""Gateway router: Resolves model aliases and dynamic adaptive routing to upstream providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional
from argus.gateway.config import GatewayConfig, ProviderConfig, TierDefinition
from argus.routing.difficulty import DifficultyClassifier


class NoEligibleProviderError(Exception):
    """Raised when no configured or authorized provider can satisfy a model request."""
    def __init__(self, requested_model: str, message: Optional[str] = None) -> None:
        self.requested_model = requested_model
        msg = message or f"No eligible inference provider configured for model '{requested_model}'."
        super().__init__(msg)


@dataclass
class RoutedTarget:
    """Resolved upstream endpoint and credentials for a model request."""
    requested_model: str
    upstream_model: str
    provider_name: str
    base_url: str
    api_key: Optional[str] = None
    kind: str = "openai_compatible"
    tier_name: str = "cheap"
    is_pinned: bool = False
    effort: str = "medium"
    decision_reason: str = ""


class GatewayRouter:
    """Routes requests to appropriate provider endpoints based on configured tiers and task difficulty."""

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self._providers = {p.name: p for p in self.config.providers}
        self.classifier = DifficultyClassifier()

    def resolve(
        self,
        requested_model: str,
        prompt: Optional[str] = None,
        messages: Optional[List[Any]] = None,
    ) -> RoutedTarget:
        model_str = (requested_model or "argus/auto").strip()
        model_lower = model_str.lower()
        if model_lower == "groq" or model_lower.startswith("groq/") or "groq.com" in model_lower:
            raise NoEligibleProviderError(
                requested_model=model_str,
                message="Groq is strictly prohibited across all of Argus.",
            )

        # 1. Dynamic Adaptive Routing for argus/auto
        tier_name: Optional[str] = None
        effort_level: str = "medium"
        reason_str: str = ""

        if model_str in ("argus/auto", "auto", "default"):
            # Extract prompt content from messages or prompt string
            text_parts: List[str] = []
            if prompt:
                text_parts.append(prompt)
            if messages:
                for m in messages:
                    if isinstance(m, dict) and m.get("role") in ("user", "system"):
                        c = m.get("content", "")
                        if isinstance(c, str):
                            text_parts.append(c)
                        elif isinstance(c, list):
                            for p in c:
                                if isinstance(p, dict) and p.get("type") == "text":
                                    text_parts.append(p.get("text", ""))

            combined_prompt = "\n".join(text_parts).strip()
            if not combined_prompt:
                tier_name = "cheap"
                effort_level = "low"
                reason_str = "Default cost-optimized starting tier"
            else:
                decision = self.classifier.classify(prompt=combined_prompt)
                effort_level = decision.effort
                reason_str = "; ".join(decision.reasons) if decision.reasons else f"Heuristic effort: {effort_level}"

                # Map effort to tier
                if decision.effort == "low":
                    tier_name = "cheap"
                elif decision.effort == "high":
                    tier_name = "frontier" if "frontier" in self.config.tiers else "strong"
                else:
                    tier_name = "strong" if "strong" in self.config.tiers else "cheap"

        elif model_str in ("argus/cheap", "cheap"):
            tier_name = "cheap"
            effort_level = "low"
            reason_str = "Explicit tier requested: cheap"
        elif model_str in ("argus/strong", "strong"):
            tier_name = "strong"
            effort_level = "medium"
            reason_str = "Explicit tier requested: strong"
        elif model_str in ("argus/frontier", "frontier"):
            tier_name = "frontier"
            effort_level = "high"
            reason_str = "Explicit tier requested: frontier"

        # Resolve tier to provider and upstream model
        if tier_name and tier_name in self.config.tiers:
            tier_def = self.config.tiers[tier_name]
            provider = self._providers.get(tier_def.provider)
            if provider:
                return RoutedTarget(
                    requested_model=model_str,
                    upstream_model=tier_def.model,
                    provider_name=provider.name,
                    base_url=provider.base_url.rstrip("/"),
                    api_key=provider.get_api_key(),
                    kind=provider.kind,
                    tier_name=tier_name,
                    is_pinned=False,
                    effort=effort_level,
                    decision_reason=reason_str,
                )

        # 2. Check if user passed an explicit pinned model from a configured tier
        for t_name, t_def in self.config.tiers.items():
            if t_def.model == model_str:
                provider = self._providers.get(t_def.provider)
                if provider:
                    return RoutedTarget(
                        requested_model=model_str,
                        upstream_model=t_def.model,
                        provider_name=provider.name,
                        base_url=provider.base_url.rstrip("/"),
                        api_key=provider.get_api_key(),
                        kind=provider.kind,
                        tier_name=t_name,
                        is_pinned=True,
                        effort=effort_level,
                        decision_reason="Direct model match",
                    )

        # 3. Explicit provider/model syntax check: e.g. "openai/gpt-4o", "anthropic/claude-3-5-sonnet"
        if "/" in model_str and not model_str.startswith("argus/"):
            req_provider, _, req_model_name = model_str.partition("/")
            req_provider_clean = req_provider.strip().lower()
            if req_provider_clean == "groq":
                raise NoEligibleProviderError(
                    requested_model=model_str,
                    message="Groq is strictly prohibited across all of Argus.",
                )
            if req_provider_clean in self._providers:
                matched_prov = self._providers[req_provider_clean]
                return RoutedTarget(
                    requested_model=model_str,
                    upstream_model=req_model_name,
                    provider_name=matched_prov.name,
                    base_url=matched_prov.base_url.rstrip("/"),
                    api_key=matched_prov.get_api_key(),
                    kind=matched_prov.kind,
                    tier_name="explicit",
                    is_pinned=True,
                    effort=effort_level,
                    decision_reason=f"Explicit provider/model request: {matched_prov.name}/{req_model_name}",
                )
            else:
                raise NoEligibleProviderError(
                    requested_model=model_str,
                    message=f"Requested provider '{req_provider}' is not configured in Argus. Configured providers: {list(self._providers.keys())}",
                )

        # 4. Fallback: If providers exist in config, use the first provider and pass the requested model through as pinned
        if self.config.providers:
            default_provider = self.config.providers[0]
            return RoutedTarget(
                requested_model=model_str,
                upstream_model=model_str,
                provider_name=default_provider.name,
                base_url=default_provider.base_url.rstrip("/"),
                api_key=default_provider.get_api_key(),
                kind=default_provider.kind,
                tier_name="default",
                is_pinned=True,
                effort=effort_level,
                decision_reason="Fallback to configured provider",
            )

        # 5. No configured providers: raise typed resolution error
        raise NoEligibleProviderError(
            requested_model=model_str,
            message=(
                f"No eligible inference provider configured for model '{model_str}'. "
                "Please configure an authorized provider via 'argus provider set' or check active gateway tiers."
            ),
        )

    def get_advertised_models(self) -> list[str]:
        """Return list of model IDs advertised at /v1/models."""
        models = [
            "argus/auto",
            "argus/cheap",
            "argus/strong",
            "argus/frontier",
        ]
        for t_def in self.config.tiers.values():
            if t_def.model and t_def.model not in models:
                models.append(t_def.model)
        return models
