"""Usage and telemetry modal dialog."""

from __future__ import annotations

from rich.table import Table
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

# Design tokens matching styles.tcss
_TEXT = "#e1e4ec"
_SECONDARY = "#9298a8"
_MUTED = "#667085"
_BLUE = "#8188ff"
_GREEN = "#7bc79e"
_ORANGE = "#e88a13"


class UsageModal(ModalScreen[None]):
    """Modal displaying session telemetry, token consumption, and cost."""

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close"),
        Binding("enter", "dismiss_modal", "Close"),
    ]

    def __init__(
        self,
        total_attempts: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_cost_usd: float = 0.0,
        budget_limit_usd: float = 0.50,
    ) -> None:
        super().__init__()
        self.total_attempts = total_attempts
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_cost_usd = total_cost_usd
        self.budget_limit_usd = budget_limit_usd

    def compose(self) -> ComposeResult:
        with Container(id="usage-container"):
            with Horizontal(classes="modal-header"):
                with Vertical(classes="modal-heading"):
                    yield Static("SPEND", classes="kicker")
                    yield Static("Session telemetry & spend", id="usage-title", classes="modal-title")
                status = f"[{_GREEN}]● active[/]" if self.total_attempts > 0 else f"[{_MUTED}]○ idle[/]"
                yield Static(status, classes="modal-status")

            yield Static(self._build_table(), id="usage-content")

            with Horizontal(classes="modal-footer"):
                yield Static("", classes="footer-hint")
                yield Button("Close (Esc)", variant="primary", id="usage-close")

    def on_mount(self) -> None:
        self.query_one("#usage-close", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def _build_table(self) -> Table:
        table = Table(show_header=False, expand=True, box=None, padding=(0, 1))
        table.add_column("Metric", style=_MUTED, width=24)
        table.add_column("Value", style=f"bold {_TEXT}")

        table.add_row("Recovery Attempts Run", str(self.total_attempts))
        table.add_row("Prompt Tokens", f"{self.prompt_tokens:,}")
        table.add_row("Completion Tokens", f"{self.completion_tokens:,}")
        table.add_row("Total Tokens", f"{self.prompt_tokens + self.completion_tokens:,}")
        table.add_row("Cumulative Session Spend", f"[bold {_GREEN}]${self.total_cost_usd:.5f}[/]")
        table.add_row("Configured Budget Limit", f"${self.budget_limit_usd:.4f}")

        pct = (self.total_cost_usd / self.budget_limit_usd * 100) if self.budget_limit_usd > 0 else 0
        filled = int(min(10, max(0, round(pct / 10))))
        bar = f"[{_GREEN}]{'█' * filled}[/][#1c1f27]{'░' * (10 - filled)}[/] {pct:.1f}%"
        table.add_row("Budget Utilization", bar)

        return table
