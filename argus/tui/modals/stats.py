"""Modal displaying live realized cost savings, routing breakdown, and token telemetry."""

from __future__ import annotations

from typing import Dict, Optional

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

# Design tokens shared with argus/tui/styles.tcss.
_TEXT = "#e1e4ec"
_SECONDARY = "#9298a8"
_MUTED = "#667085"
_BLUE = "#8188ff"
_GREEN = "#7bc79e"
_ORANGE = "#e88a13"
_RED = "#e07178"
_BORDER = "#252831"


def _stat_block(label: str, value: str, unit: str, *, primary: bool = False, value_style: str = "") -> Vertical:
    """Build one hero metric block (label / value / unit)."""
    css = "stat-block stat-primary" if primary else "stat-block stat-secondary"
    value_css = f"stat-value {value_style}" if value_style else "stat-value"
    return Vertical(
        Static(label.upper(), classes="stat-label"),
        Static(value, classes=value_css),
        Static(unit, classes="stat-unit"),
        classes=css,
    )


class StatsModal(ModalScreen[None]):
    """Modal displaying session telemetry, alpha savings vs frontier, and tier distribution."""

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close"),
        Binding("enter", "dismiss_modal", "Close"),
    ]

    def __init__(
        self,
        total_calls: Optional[int] = None,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        session_spend_usd: Optional[float] = None,
        frontier_baseline_usd: Optional[float] = None,
        budget_limit_usd: float = 0.50,
    ) -> None:
        super().__init__()
        from argus.gateway.telemetry import TelemetryRegistry

        summary = TelemetryRegistry.get().get_summary()

        self.total_calls = total_calls if total_calls is not None else summary.total_calls
        self.prompt_tokens = prompt_tokens if prompt_tokens is not None else summary.prompt_tokens
        self.completion_tokens = completion_tokens if completion_tokens is not None else summary.completion_tokens
        self.session_spend_usd = session_spend_usd if session_spend_usd is not None else summary.session_spend_usd
        self.uncertain_spend_usd = getattr(summary, "uncertain_spend_usd", 0.0)
        self.frontier_baseline_usd = (
            frontier_baseline_usd if frontier_baseline_usd is not None else summary.frontier_cost_usd
        )
        self.budget_limit_usd = budget_limit_usd
        self.tier_counts: Dict[str, int] = summary.tier_counts
        self.active_client: Optional[str] = summary.active_client

    # ---- derived metrics -------------------------------------------------
    @property
    def _is_idle(self) -> bool:
        return self.total_calls == 0

    def _saved_usd(self) -> float:
        return max(0.0, self.frontier_baseline_usd - self.session_spend_usd)

    def _pct_saved(self) -> float:
        if self.frontier_baseline_usd <= 0:
            return 0.0
        return self._saved_usd() / self.frontier_baseline_usd * 100

    def _pct_budget(self) -> float:
        if self.budget_limit_usd <= 0:
            return 0.0
        return self.session_spend_usd / self.budget_limit_usd * 100

    def _tier_split(self) -> str:
        cheap = self.tier_counts.get("cheap", 0)
        strong = self.tier_counts.get("strong", 0)
        frontier = self.tier_counts.get("frontier", 0)
        return f"t1 {cheap} · t2 {strong} · t3 {frontier}"

    # ---- compose ---------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Container(id="stats-container"):
            with Horizontal(id="stats-head"):
                with Vertical(classes="modal-heading"):
                    yield Static("SESSION TELEMETRY", classes="kicker")
                    yield Static(
                        "Cost & routing economics" if not self._is_idle else "Gateway is idle",
                        id="stats-title",
                    )
                yield Static(
                    "● LIVE" if not self._is_idle else "○ IDLE",
                    id="stats-status",
                )

            with Horizontal(id="stats-hero"):
                if self._is_idle:
                    yield _stat_block(
                        "Endpoint",
                        "127.0.0.1:8000/v1",
                        "gateway listening",
                        primary=True,
                        value_style="-blue",
                    )
                    yield _stat_block(
                        "Session spend",
                        "$0.0000",
                        f"of ${self.budget_limit_usd:.2f} cap",
                    )
                    yield _stat_block(
                        "Traffic",
                        "0 calls",
                        "awaiting requests",
                    )
                else:
                    yield _stat_block(
                        "Session spend",
                        f"${self.session_spend_usd:.4f}",
                        f"of ${self.budget_limit_usd:.2f} cap",
                        primary=True,
                        value_style="-blue",
                    )
                    yield _stat_block(
                        "Routed requests",
                        f"{self.total_calls}",
                        f"calls · {self._tier_split()}",
                    )
                    yield _stat_block(
                        "Total tokens",
                        f"{self.prompt_tokens + self.completion_tokens:,}",
                        f"{self.prompt_tokens:,} in · {self.completion_tokens:,} out",
                    )

            yield Static(self._build_budget_bar(), id="stats-budget-bar")
            yield Static(self._build_table(), id="stats-table")

            with Horizontal(classes="modal-footer"):
                yield Static("", classes="footer-hint")
                yield Button("Close (Esc)", variant="primary", id="btn-stats-close")

    def on_mount(self) -> None:
        self.query_one("#btn-stats-close", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    # ---- rich renderables ------------------------------------------------
    def _build_budget_bar(self) -> Text:
        pct = self._pct_budget()
        filled = int(min(20, max(0, round(pct / 5))))
        bar = Text()
        bar.append("BUDGET  ", style=_MUTED)
        color = _GREEN if pct < 70 else (_ORANGE if pct < 95 else _RED)
        bar.append("█" * filled, style=color)
        bar.append("░" * (20 - filled), style="#252831")
        bar.append(f"  {pct:.1f}% of ${self.budget_limit_usd:.2f}", style=_SECONDARY)
        return bar

    def _build_table(self) -> Table:
        table = Table(show_header=False, expand=True, box=None, padding=(0, 1))
        table.add_column("Metric", style=_MUTED, width=24)
        table.add_column("Value", style=_TEXT)

        if self._is_idle:
            table.add_row("Status", f"[{_GREEN}]● Gateway ready[/]  [{_SECONDARY}]127.0.0.1:8000/v1[/]")
            table.add_row("Active Client", f"[{_SECONDARY}]Listening for inbound SDK/agent traffic[/]")
            table.add_row("Routing Policy", f"[{_SECONDARY}]Adaptive escalation (T1 → T2 → T3)[/]")
            table.add_row(
                "Quick setup",
                f"[{_MUTED}]Type /connect in the TUI, or run 'argus init aider' / 'argus setup'.[/]",
            )
            return table

        table.add_row("Active Client", f"[bold {_BLUE}]{self.active_client or 'api-client'}[/]")
        table.add_row("Total routed requests", f"[bold {_TEXT}]{self.total_calls}[/] calls")

        cheap_cnt = self.tier_counts.get("cheap", 0)
        strong_cnt = self.tier_counts.get("strong", 0)
        frontier_cnt = self.tier_counts.get("frontier", 0)
        total_tiers = sum(self.tier_counts.values()) if self.tier_counts else 0
        tot = max(1, total_tiers if total_tiers > 0 else self.total_calls)

        # Kept short enough to stay on one row inside the detail column.
        table.add_row(
            "Tier 1 (Cheap 8B)",
            f"[{_GREEN}]{cheap_cnt} · {cheap_cnt / tot * 100:.0f}%[/]   "
            f"[{_BLUE}]T2 {strong_cnt} · {strong_cnt / tot * 100:.0f}%[/]   "
            f"[{_ORANGE}]T3 {frontier_cnt} · {frontier_cnt / tot * 100:.0f}%[/]",
        )
        table.add_row(
            "Tokens (in / out)",
            f"[{_TEXT}]{self.prompt_tokens:,}[/] / [{_TEXT}]{self.completion_tokens:,}[/]",
        )
        if self.uncertain_spend_usd > 0:
            table.add_row("Uncertain / at-risk", f"[{_ORANGE}]${self.uncertain_spend_usd:.4f}[/]")

        return table
