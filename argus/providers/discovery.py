"""Live model discovery and capability routing registry for Argus.

Discovers accessible inference models across:
1. Antigravity Host Native CLI (authenticated host environment, zero external keys)
2. Configured API Providers (OpenAI, DeepSeek, OpenRouter) via Keyring and environment
3. Local inference runtimes (Ollama, LM Studio)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from argus.providers.antigravity import AntigravityHostAdapter, is_antigravity_cli_available
from argus.providers.storage import ProviderStorage

logger = logging.getLogger("argus.providers.discovery")

# Human-friendly descriptions and tier mappings for well-known models
MODEL_METADATA: Dict[str, Dict[str, str]] = {
    # Antigravity Host Native
    "gemini-3.8-flash-low": {
        "name": "Gemini 3.8 Flash (Low Effort)",
        "tier": "fast",
        "desc": "Ultra-fast · Host Native",
    },
    "gemini-3.8-flash-medium": {
        "name": "Gemini 3.8 Flash (Medium Effort)",
        "tier": "fast",
        "desc": "Balanced Fast · Host Native",
    },
    "gemini-3.8-flash-high": {
        "name": "Gemini 3.8 Flash (High Effort)",
        "tier": "strong",
        "desc": "Deep Reasoning · Host Native",
    },
    "claude-sonnet-4-6": {
        "name": "Claude Sonnet 4.6 (Thinking)",
        "tier": "strong",
        "desc": "Code Reasoning · Host Native",
    },
    "claude-opus-4-6-thinking": {
        "name": "Claude Opus 4.6 (Thinking)",
        "tier": "frontier",
        "desc": "Frontier · Host Native",
    },
    "gpt-oss-120b-medium": {
        "name": "GPT-OSS 120B (Medium)",
        "tier": "strong",
        "desc": "Open Weights 120B · Host",
    },
    "gemini-2.5-flash": {
        "name": "Gemini 2.5 Flash",
        "tier": "fast",
        "desc": "Fast · Google Gemini",
    },
    "gemini-2.5-pro": {
        "name": "Gemini 2.5 Pro",
        "tier": "strong",
        "desc": "Reasoning · Google Gemini",
    },
    # OpenAI API
    "gpt-4o-mini": {
        "name": "GPT-4o Mini",
        "tier": "fast",
        "desc": "Fast & Cheap · OpenAI",
    },
    "gpt-4o": {
        "name": "GPT-4o",
        "tier": "strong",
        "desc": "Flagship · OpenAI",
    },
    "o3-mini": {
        "name": "o3-mini",
        "tier": "frontier",
        "desc": "Frontier · OpenAI",
    },
    # DeepSeek API
    "deepseek-chat": {
        "name": "DeepSeek-V3",
        "tier": "strong",
        "desc": "General Purpose · DeepSeek",
    },
    "deepseek-reasoner": {
        "name": "DeepSeek-R1",
        "tier": "frontier",
        "desc": "R1 Reasoning · DeepSeek",
    },
    # Local Ollama
    "qwen2.5-coder:7b": {
        "name": "Qwen 2.5 Coder 7B",
        "tier": "fast",
        "desc": "Local Endpoint · Ollama",
    },
    # Groq API
    "llama-3.3-70b-versatile": {
        "name": "Llama 3.3 70B Versatile",
        "tier": "strong",
        "desc": "Fast LPU · Groq",
    },
    "qwen-2.5-coder-32b": {
        "name": "Qwen 2.5 Coder 32B",
        "tier": "strong",
        "desc": "Code LPU · Groq",
    },
    "llama-3.1-8b-instant": {
        "name": "Llama 3.1 8B Instant",
        "tier": "fast",
        "desc": "Ultra-fast · Groq",
    },
}


@dataclass
class DiscoveredModel:
    """Represents a discovered model and its live availability state."""
    id: str
    name: str
    provider: str
    tier: str  # 'fast', 'strong', 'frontier'
    is_available: bool
    status_label: str
    status_tag: str  # 'ready_host', 'ready_key', 'needs_key', 'offline'
    description: str

    @property
    def model_id(self) -> str:
        return self.id

    @property
    def display_line(self) -> str:
        """Formatted line for TUI option selection."""
        status_badge = {
            "ready_host": "[bold #3fb950]● Ready (Host)[/]",
            "ready_key": "[bold #818cf8]● Ready (Key)[/]",
            "needs_key": "[dim #64748b]○ Needs Key[/]",
            "offline": "[dim #64748b]○ Offline[/]",
        }.get(self.status_tag, "[dim #64748b]○ Unverified[/]")

        return f"{status_badge}  {self.id:<25}  [dim #94a3b8]{self.description}[/]"


_cached_discovery: Optional[List[DiscoveredModel]] = None


def discover_all_models(
    storage: Optional[ProviderStorage] = None,
    force_refresh: bool = False,
    probe_ollama: bool = True,
) -> List[DiscoveredModel]:
    """Probe the host and storage for all available inference models."""
    global _cached_discovery
    if _cached_discovery is not None and not force_refresh:
        return list(_cached_discovery)

    storage = storage or ProviderStorage()
    models: List[DiscoveredModel] = []
    seen_ids: set[str] = set()

    # 1. Antigravity Host Native CLI
    has_host_cli = is_antigravity_cli_available()
    if has_host_cli:
        try:
            adapter = AntigravityHostAdapter()
            caps = adapter.discover_capabilities()
            host_model_ids = caps.get("models") or []
        except Exception:
            host_model_ids = []

        if not host_model_ids:
            # Fallback to supported native default aliases
            host_model_ids = [
                "gemini-3.8-flash-medium",
                "claude-sonnet-4-6",
                "claude-opus-4-6-thinking",
                "gpt-oss-120b-medium",
                "gemini-2.5-flash",
                "gemini-2.5-pro",
            ]

        for mid in host_model_ids:
            meta = MODEL_METADATA.get(mid, {
                "name": mid,
                "tier": "fast" if "flash" in mid or "mini" in mid else ("frontier" if "opus" in mid or "r1" in mid else "strong"),
                "desc": "Host Native (Zero external keys required)",
            })
            models.append(
                DiscoveredModel(
                    id=mid,
                    name=meta["name"],
                    provider="antigravity",
                    tier=meta["tier"],
                    is_available=True,
                    status_label="Ready · Host Native",
                    status_tag="ready_host",
                    description=meta["desc"],
                )
            )
            seen_ids.add(mid)

    # 2. OpenAI API
    has_openai_key = bool(storage.get_api_key("openai"))
    openai_models = ["gpt-4o-mini", "gpt-4o"]
    for mid in openai_models:
        if mid not in seen_ids:
            meta = MODEL_METADATA.get(mid, {"name": mid, "tier": "strong", "desc": "OpenAI API"})
            models.append(
                DiscoveredModel(
                    id=mid,
                    name=meta["name"],
                    provider="openai",
                    tier=meta["tier"],
                    is_available=has_openai_key,
                    status_label="Ready · API Key" if has_openai_key else "Needs API Key",
                    status_tag="ready_key" if has_openai_key else "needs_key",
                    description=meta["desc"],
                )
            )
            seen_ids.add(mid)

    # 3. DeepSeek API
    has_deepseek_key = bool(storage.get_api_key("deepseek"))
    deepseek_models = ["deepseek-chat", "deepseek-reasoner"]
    for mid in deepseek_models:
        if mid not in seen_ids:
            meta = MODEL_METADATA.get(mid, {"name": mid, "tier": "strong", "desc": "DeepSeek API"})
            models.append(
                DiscoveredModel(
                    id=mid,
                    name=meta["name"],
                    provider="deepseek",
                    tier=meta["tier"],
                    is_available=has_deepseek_key,
                    status_label="Ready · API Key" if has_deepseek_key else "Needs API Key",
                    status_tag="ready_key" if has_deepseek_key else "needs_key",
                    description=meta["desc"],
                )
            )
            seen_ids.add(mid)

    # 4. OpenRouter API
    has_openrouter_key = bool(storage.get_api_key("openrouter"))
    openrouter_models = ["anthropic/claude-3.5-sonnet", "meta-llama/llama-3.3-70b-instruct"]
    for mid in openrouter_models:
        if mid not in seen_ids:
            meta = MODEL_METADATA.get(mid, {"name": mid.split("/")[-1], "tier": "strong", "desc": "OpenRouter API"})
            models.append(
                DiscoveredModel(
                    id=mid,
                    name=meta["name"],
                    provider="openrouter",
                    tier=meta["tier"],
                    is_available=has_openrouter_key,
                    status_label="Ready · API Key" if has_openrouter_key else "Needs API Key",
                    status_tag="ready_key" if has_openrouter_key else "needs_key",
                    description=meta["desc"],
                )
            )
            seen_ids.add(mid)

    # 5. Groq API
    has_groq_key = bool(storage.get_api_key("groq"))
    groq_models = ["llama-3.3-70b-versatile", "qwen-2.5-coder-32b", "llama-3.1-8b-instant"]
    for mid in groq_models:
        if mid not in seen_ids:
            meta = MODEL_METADATA.get(mid, {"name": mid, "tier": "strong", "desc": "Groq Cloud API"})
            models.append(
                DiscoveredModel(
                    id=mid,
                    name=meta["name"],
                    provider="groq",
                    tier=meta["tier"],
                    is_available=has_groq_key,
                    status_label="Ready · API Key" if has_groq_key else "Needs API Key",
                    status_tag="ready_key" if has_groq_key else "needs_key",
                    description=meta["desc"],
                )
            )
            seen_ids.add(mid)

    # 6. Google AI Studio (Gemini API)
    has_gemini_key = bool(storage.get_api_key("gemini") or storage.get_api_key("google"))
    if not has_host_cli:
        gemini_models = ["gemini-2.5-flash", "gemini-2.5-pro"]
        for mid in gemini_models:
            if mid not in seen_ids:
                meta = MODEL_METADATA.get(mid, {"name": mid, "tier": "fast", "desc": "Google AI Studio"})
                models.append(
                    DiscoveredModel(
                        id=mid,
                        name=meta["name"],
                        provider="gemini",
                        tier=meta["tier"],
                        is_available=has_gemini_key,
                        status_label="Ready · API Key" if has_gemini_key else "Needs API Key",
                        status_tag="ready_key" if has_gemini_key else "needs_key",
                        description=meta["desc"],
                    )
                )
                seen_ids.add(mid)

    # 7. Local Ollama
    ollama_online = False
    if probe_ollama:
        try:
            import httpx
            resp = httpx.get("http://localhost:11434/api/tags", timeout=0.3)
            if resp.status_code == 200:
                ollama_online = True
                tags_data = resp.json().get("models", [])
                for tag_obj in tags_data:
                    t_name = tag_obj.get("name")
                    if t_name and t_name not in seen_ids:
                        models.append(
                            DiscoveredModel(
                                id=t_name,
                                name=f"Ollama {t_name}",
                                provider="ollama",
                                tier="fast",
                                is_available=True,
                                status_label="Ready · Local Ollama",
                                status_tag="ready_key",
                                description="Local Endpoint · Zero Cost Ollama",
                            )
                        )
                        seen_ids.add(t_name)
        except Exception:
            ollama_online = False

    if not ollama_online and "qwen2.5-coder:7b" not in seen_ids:
        models.append(
            DiscoveredModel(
                id="qwen2.5-coder:7b",
                name="Qwen 2.5 Coder 7B",
                provider="ollama",
                tier="fast",
                is_available=False,
                status_label="Offline · Ollama",
                status_tag="offline",
                description="Local Endpoint (Ollama not running)",
            )
        )

    _cached_discovery = models
    return list(models)


def resolve_dynamic_tiers(
    discovered: Optional[List[DiscoveredModel]] = None,
) -> Dict[str, Dict[str, str]]:
    """Resolve active T1 FAST, T2 STRONG, and T3 FRONTIER models based on real availability."""
    models = discovered or discover_all_models()
    avail = [m for m in models if m.is_available]

    # Defaults
    t1_model = "gemini-3.8-flash-medium"
    t2_model = "claude-sonnet-4-6"
    t3_model = "claude-opus-4-6-thinking"

    # Select T1 (Fast)
    fast_cands = [m for m in avail if m.tier == "fast"]
    if fast_cands:
        # Prefer flash-medium or flash-low or gpt-4o-mini
        pref_order = ["gemini-3.8-flash-medium", "gemini-3.8-flash-low", "gemini-2.5-flash", "gpt-4o-mini"]
        found = False
        for p in pref_order:
            for m in fast_cands:
                if m.id == p:
                    t1_model = m.id
                    found = True
                    break
            if found:
                break
        if not found:
            t1_model = fast_cands[0].id
    elif avail:
        t1_model = avail[0].id

    # Select T2 (Strong)
    strong_cands = [m for m in avail if m.tier == "strong"]
    if strong_cands:
        pref_order = ["claude-sonnet-4-6", "gemini-3.8-flash-high", "gpt-4o", "deepseek-chat", "gpt-oss-120b-medium"]
        found = False
        for p in pref_order:
            for m in strong_cands:
                if m.id == p:
                    t2_model = m.id
                    found = True
                    break
            if found:
                break
        if not found:
            t2_model = strong_cands[0].id
    elif len(avail) > 1:
        t2_model = avail[1].id
    else:
        t2_model = t1_model

    # Select T3 (Frontier)
    frontier_cands = [m for m in avail if m.tier == "frontier"]
    if frontier_cands:
        pref_order = ["claude-opus-4-6-thinking", "deepseek-reasoner", "o3-mini", "gpt-4o"]
        found = False
        for p in pref_order:
            for m in frontier_cands:
                if m.id == p:
                    t3_model = m.id
                    found = True
                    break
            if found:
                break
        if not found:
            t3_model = frontier_cands[0].id
    else:
        # If no frontier model available, use best strong model
        t3_model = t2_model

    return {
        "t1": {"name": "T1 FAST", "model": t1_model},
        "t2": {"name": "T2 STRONG", "model": t2_model},
        "t3": {"name": "T3 FRONTIER", "model": t3_model},
    }
