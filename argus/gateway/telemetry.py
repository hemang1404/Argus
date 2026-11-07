"""Argus Gateway Telemetry & Client Session Registry.

Tracks real-time client traffic, dynamic tier routing decisions, token counts,
realized USD spend, and realized alpha cost savings vs frontier models.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from argus.config import MODEL_REGISTRY, ModelCost

# Baseline reference frontier model pricing ($3.00 / $15.00 per M tokens)
FRONTIER_BASELINE = ModelCost(input_cost_per_m=3.00, output_cost_per_m=15.00)


def detect_client(client_header: Optional[str] = None, user_agent: Optional[str] = None) -> str:
    """Identify the external AI agent / tool from HTTP headers."""
    if client_header and client_header.strip():
        return client_header.strip().lower()

    ua = (user_agent or "").lower()
    if "aider" in ua:
        return "aider"
    if "cursor" in ua:
        return "cursor"
    if "opencode" in ua:
        return "opencode"
    if "claude" in ua or "anthropic" in ua:
        return "claude-code"
    if "continue" in ua:
        return "continue"
    if "antigravity" in ua:
        return "antigravity"
    if any(cli in ua for cli in ("curl", "python", "httpx", "urllib", "requests")):
        return "api-client"
    return "external-client"


@dataclass
class TelemetryEvent:
    """Record of a single routed inference turn."""
    timestamp: str
    client: str
    requested_model: str
    routed_model: str
    tier: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    cost_usd: float
    frontier_cost_usd: float
    alpha_saved_usd: float
    alpha_percent: float
    usage_source: str = "provider_reported"  # "provider_reported" | "locally_estimated" | "unknown"
    cost_status: str = "estimated"          # "measured" | "estimated" | "zero_subscription" | "uncertain"
    uncertain_usd: float = 0.0


@dataclass
class SessionSummary:
    """Aggregated session metrics for TUI and CLI telemetry."""
    active_client: Optional[str] = None
    total_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    session_spend_usd: float = 0.0
    uncertain_spend_usd: float = 0.0
    frontier_cost_usd: float = 0.0
    alpha_saved_usd: float = 0.0
    alpha_percent: float = 0.0
    tier_counts: Dict[str, int] = field(default_factory=lambda: {"cheap": 0, "strong": 0, "frontier": 0})
    recent_events: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def total_prompt_tokens(self) -> int:
        return self.prompt_tokens

    @property
    def total_completion_tokens(self) -> int:
        return self.completion_tokens


class TelemetryRegistry:
    """Thread-safe persistent store for routing telemetry."""

    _instance: Optional[TelemetryRegistry] = None
    _lock = threading.Lock()

    def __init__(self, workspace_root: Path | str = ".") -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.dot_argus = self.workspace_root / ".argus"
        self.events_file = self.dot_argus / "telemetry_events.jsonl"
        self.session_file = self.dot_argus / "session.json"
        self._summary = SessionSummary()
        self._ensure_storage()
        self._load_existing()

    @classmethod
    def get(cls, workspace_root: Path | str = ".") -> TelemetryRegistry:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(workspace_root)
            return cls._instance

    def _ensure_storage(self) -> None:
        try:
            self.dot_argus.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def _load_existing(self) -> None:
        """Load session state if file exists."""
        if not self.session_file.exists():
            return
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
            self._summary = SessionSummary(
                active_client=data.get("active_client"),
                total_calls=data.get("total_calls", 0),
                prompt_tokens=data.get("prompt_tokens", 0),
                completion_tokens=data.get("completion_tokens", 0),
                session_spend_usd=data.get("session_spend_usd", 0.0),
                uncertain_spend_usd=data.get("uncertain_spend_usd", 0.0),
                frontier_cost_usd=data.get("frontier_cost_usd", 0.0),
                alpha_saved_usd=data.get("alpha_saved_usd", 0.0),
                alpha_percent=data.get("alpha_percent", 0.0),
                tier_counts=data.get("tier_counts", {"cheap": 0, "strong": 0, "frontier": 0}),
                recent_events=data.get("recent_events", []),
            )
        except Exception:
            pass

    def record_turn(
        self,
        client: str,
        requested_model: str,
        routed_model: str,
        tier: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        cost_usd: float,
        usage_source: str = "provider_reported",
        cost_status: str = "estimated",
        uncertain_usd: float = 0.0,
    ) -> TelemetryEvent:
        """Record an inference event, calculate realized alpha, and persist to disk."""
        frontier_cost = FRONTIER_BASELINE.calculate_cost(prompt_tokens, completion_tokens)
        
        # If uncertain outcome, alpha savings cannot be confirmed
        if cost_status == "uncertain":
            alpha_saved = 0.0
            alpha_pct = 0.0
            if uncertain_usd == 0.0 and cost_usd > 0.0:
                uncertain_usd = cost_usd
        else:
            alpha_saved = max(0.0, round(frontier_cost - cost_usd, 6))
            alpha_pct = round((alpha_saved / frontier_cost) * 100.0, 1) if frontier_cost > 0 else 0.0

        ts_str = datetime.now().strftime("%H:%M:%S")
        event = TelemetryEvent(
            timestamp=ts_str,
            client=client,
            requested_model=requested_model,
            routed_model=routed_model,
            tier=tier,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=round(latency_ms, 1),
            cost_usd=round(cost_usd, 6),
            frontier_cost_usd=round(frontier_cost, 6),
            alpha_saved_usd=alpha_saved,
            alpha_percent=alpha_pct,
            usage_source=usage_source,
            cost_status=cost_status,
            uncertain_usd=round(uncertain_usd, 6),
        )

        with self._lock:
            # Update summary metrics
            s = self._summary
            s.active_client = client
            s.total_calls += 1
            s.prompt_tokens += prompt_tokens
            s.completion_tokens += completion_tokens
            if cost_status == "uncertain":
                s.uncertain_spend_usd = round(s.uncertain_spend_usd + uncertain_usd, 6)
            else:
                s.session_spend_usd = round(s.session_spend_usd + cost_usd, 6)
            s.frontier_cost_usd = round(s.frontier_cost_usd + frontier_cost, 6)
            s.alpha_saved_usd = round(s.alpha_saved_usd + alpha_saved, 6)
            s.alpha_percent = round((s.alpha_saved_usd / s.frontier_cost_usd) * 100.0, 1) if s.frontier_cost_usd > 0 else 0.0

            normalized_tier = tier.lower() if tier.lower() in s.tier_counts else "cheap"
            s.tier_counts[normalized_tier] = s.tier_counts.get(normalized_tier, 0) + 1

            # Keep last 20 events in memory
            event_dict = asdict(event)
            s.recent_events.insert(0, event_dict)
            if len(s.recent_events) > 20:
                s.recent_events.pop()

            # Persist summary
            try:
                self.session_file.write_text(json.dumps(asdict(s), indent=2), encoding="utf-8")
                with open(self.events_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event_dict) + "\n")
            except Exception:
                pass

        return event

    def get_summary(self) -> SessionSummary:
        """Return the current session summary."""
        with self._lock:
            return self._summary

    def clear(self) -> None:
        """Reset session metrics and wipe session file."""
        with self._lock:
            self._summary = SessionSummary()
            try:
                if self.session_file.exists():
                    self.session_file.unlink()
                if self.events_file.exists():
                    self.events_file.unlink()
            except Exception:
                pass
