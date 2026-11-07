"""Routing & Escalation Policy Modal inspired by Linear/Raycast minimalist ergonomics."""

from __future__ import annotations

from typing import Any, Optional

from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from argus.config import ArgusConfig
from argus.providers.discovery import resolve_dynamic_tiers

_TEXT = "#e1e4ec"
_SECONDARY = "#9298a8"
_MUTED = "#667085"
_BLUE = "#8188ff"
_GREEN = "#7bc79e"
_ORANGE = "#e88a13"
_RED = "#e07178"


def _clean_card_label(m: str) -> str:
    if len(m) <= 20:
        return m
    if m.startswith("gemini-3.8-flash"):
        return "gemini-3.8-flash"
    if m.startswith("claude-opus-4-6"):
        return "claude-opus-4-6"
    return m[:20]


class PolicyModal(ModalScreen[Optional[dict[str, Any]]]):
    """Modal dialog displaying tiered model routing and budget governance."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("1", "select_tier_1", "Select T1", show=False),
        Binding("2", "select_tier_2", "Select T2", show=False),
        Binding("3", "select_tier_3", "Select T3", show=False),
        Binding("a", "select_tier_auto", "Select Auto", show=False),
        Binding("enter", "save_policy", "Apply", show=False),
    ]

    def __init__(
        self,
        current_config: ArgusConfig,
        current_tier: str = "argus/auto",
    ) -> None:
        super().__init__()
        self.config = current_config
        self.selected_tier = current_tier
        # Use cached tiers if already available, otherwise load defaults immediately
        # and refresh asynchronously in on_mount to avoid blocking the UI thread.
        from argus.providers.discovery import _cached_discovery
        if _cached_discovery is not None:
            tiers = resolve_dynamic_tiers(_cached_discovery)
        else:
            tiers = {
                "t1": {"name": "T1 FAST", "model": "gemini-3.8-flash-medium"},
                "t2": {"name": "T2 STRONG", "model": "claude-sonnet-4-6"},
                "t3": {"name": "T3 FRONTIER", "model": "claude-opus-4-6-thinking"},
            }
        self.t1_model = tiers["t1"]["model"]
        self.t2_model = tiers["t2"]["model"]
        self.t3_model = tiers["t3"]["model"]

    def _status_badge(self) -> str:
        if self.selected_tier in ("argus/auto", "auto", ""):
            return f"[{_GREEN}]● auto ladder[/]"
        return f"[{_BLUE}]● pinned tier[/]"

    def compose(self) -> ComposeResult:
        with Container(id="policy-container"):
            with Horizontal(id="policy-header"):
                yield Static("Routing & escalation", id="policy-title")
                yield Static(self._status_badge(), id="policy-esc")

            with Horizontal(id="policy-cards"):
                with Vertical(id="tier-card-1", classes="tier-card"):
                    with Horizontal(classes="tier-card-top"):
                        yield Static("T1", classes="tier-id")
                        yield Static("FAST", classes="tier-card-title")
                        yield Static("", classes="tier-check")
                    yield Static(_clean_card_label(self.t1_model), classes="tier-card-model")
                    yield Static("first attempt", classes="tier-card-note")

                with Vertical(id="tier-card-2", classes="tier-card"):
                    with Horizontal(classes="tier-card-top"):
                        yield Static("T2", classes="tier-id")
                        yield Static("STRONG", classes="tier-card-title")
                        yield Static("", classes="tier-check")
                    yield Static(_clean_card_label(self.t2_model), classes="tier-card-model")
                    yield Static("on failure", classes="tier-card-note")

                with Vertical(id="tier-card-3", classes="tier-card"):
                    with Horizontal(classes="tier-card-top"):
                        yield Static("T3", classes="tier-id")
                        yield Static("FRONTIER", classes="tier-card-title")
                        yield Static("", classes="tier-check")
                    yield Static(_clean_card_label(self.t3_model), classes="tier-card-model")
                    yield Static("last resort", classes="tier-card-note")

            with Horizontal(id="policy-fields"):
                with Vertical(classes="policy-field"):
                    yield Static("BUDGET CEILING (USD)", classes="field-label")
                    yield Input(
                        value=f"{self.config.budget_limit_usd:.2f}",
                        id="config-input",
                        placeholder="0.03",
                    )

                with Vertical(classes="policy-field"):
                    yield Static("MAX ATTEMPTS", classes="field-label")
                    yield Input(
                        value=str(self.config.max_attempts),
                        id="policy-attempts-input",
                        placeholder="3",
                    )

                with Vertical(classes="policy-field"):
                    yield Static("SANDBOX", classes="field-label")
                    yield Static("git worktree", classes="policy-static-value")

            yield Static("", id="config-error")

            with Horizontal(classes="modal-footer"):
                yield Static(
                    f"[{_MUTED}]1/2/3 pin tier  ·  a auto[/]",
                    id="policy-key-hints",
                )
                yield Button("Cancel (Esc)", id="btn-cancel")
                yield Button("Apply (Enter)", variant="primary", id="btn-apply")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button clicks."""
        if event.button.id == "btn-cancel":
            self.action_cancel()
        elif event.button.id == "btn-apply":
            self.action_save_policy()

    def on_click(self, event: events.Click) -> None:
        """Allow clicking directly on tier cards to switch tiers."""
        widget = getattr(event, "widget", None)
        if widget is None:
            return
        ancestors = getattr(widget, "ancestors_with_self", None)
        nodes = list(ancestors) if ancestors is not None else [widget] + list(getattr(widget, "ancestors", []))
        for node in nodes:
            node_id = getattr(node, "id", None)
            if node_id == "tier-card-1":
                self.action_select_tier_1()
                return
            elif node_id == "tier-card-2":
                self.action_select_tier_2()
                return
            elif node_id == "tier-card-3":
                self.action_select_tier_3()
                return

    def on_mount(self) -> None:
        """Update active card selection highlight and refresh tiers from background discovery."""
        self._refresh_card_selection()
        self.query_one("#config-input", Input).focus()
        # If we opened with defaults (no cache), trigger background refresh
        from argus.providers.discovery import _cached_discovery
        if _cached_discovery is None:
            self._refresh_tiers_async()

    @work(thread=True, exclusive=True)
    def _refresh_tiers_async(self) -> None:
        """Discover available models off the UI thread and update cards."""
        try:
            tiers = resolve_dynamic_tiers()
        except Exception:
            return
        t1 = tiers["t1"]["model"]
        t2 = tiers["t2"]["model"]
        t3 = tiers["t3"]["model"]
        # Only update if something actually changed from defaults
        if t1 == self.t1_model and t2 == self.t2_model and t3 == self.t3_model:
            return
        self.t1_model = t1
        self.t2_model = t2
        self.t3_model = t3
        self.app.call_from_thread(self._apply_discovered_tiers)

    def _apply_discovered_tiers(self) -> None:
        """Update tier card model labels after background discovery completes."""
        if not self.is_mounted:
            return
        try:
            cards = self.query(".tier-card-model")
            labels = [_clean_card_label(m) for m in (self.t1_model, self.t2_model, self.t3_model)]
            for card, label in zip(cards, labels):
                card.update(label)
        except Exception:
            pass

    def _refresh_card_selection(self) -> None:
        c1 = self.query_one("#tier-card-1", Vertical)
        c2 = self.query_one("#tier-card-2", Vertical)
        c3 = self.query_one("#tier-card-3", Vertical)

        for card in (c1, c2, c3):
            card.remove_class("-selected")

        is_auto = self.selected_tier in ("argus/auto", "auto", "")

        if self.selected_tier in ("argus/cheap", "t1"):
            selected = [True, False, False]
        elif self.selected_tier in ("argus/strong", "t2"):
            selected = [False, True, False]
        elif self.selected_tier in ("argus/frontier", "t3"):
            selected = [False, False, True]
        else:
            selected = [True, True, True]

        for card, active in zip((c1, c2, c3), selected):
            if active:
                card.add_class("-selected")

        checks = list(self.query(".tier-check"))
        if is_auto:
            indicators = [f"[{_GREEN}]1st[/]", f"[{_BLUE}]2nd[/]", f"[{_ORANGE}]3rd[/]"]
            for check, ind in zip(checks, indicators):
                check.update(ind)
        else:
            for check, active in zip(checks, selected):
                check.update(f"[{_BLUE}]✓[/]" if active else "")

        if self.query("#policy-esc"):
            self.query_one("#policy-esc", Static).update(self._status_badge())

    def action_select_tier_1(self) -> None:
        self.selected_tier = "argus/cheap"
        self._refresh_card_selection()

    def action_select_tier_2(self) -> None:
        self.selected_tier = "argus/strong"
        self._refresh_card_selection()

    def action_select_tier_3(self) -> None:
        self.selected_tier = "argus/frontier"
        self._refresh_card_selection()

    def action_select_tier_auto(self) -> None:
        self.selected_tier = "argus/auto"
        self._refresh_card_selection()

    def action_save_policy(self) -> None:
        self._save_policy()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._save_policy()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _save_policy(self) -> None:
        raw_budget = self.query_one("#config-input", Input).value.strip()
        raw_attempts = self.query_one("#policy-attempts-input", Input).value.strip()
        err = self.query_one("#config-error", Static)

        # Parse budget
        clean_b = raw_budget.replace("$", "").replace("USD", "").strip()
        try:
            b_val = float(clean_b)
            if not (0.01 <= b_val <= 10.0):
                err.update(f"[{_RED}]Budget must be between $0.01 and $10.00 USD[/]")
                return
        except ValueError:
            err.update(f"[{_RED}]Invalid budget number format[/]")
            return

        # Parse attempts
        clean_a = raw_attempts.replace("turns", "").strip()
        try:
            a_val = int(clean_a)
            if not (1 <= a_val <= 10):
                err.update(f"[{_RED}]Attempts must be between 1 and 10[/]")
                return
        except ValueError:
            err.update(f"[{_RED}]Invalid attempt count[/]")
            return

        self.dismiss({
            "tier": self.selected_tier,
            "budget": b_val,
            "attempts": a_val,
            "t1_model": self.t1_model,
            "t2_model": self.t2_model,
            "t3_model": self.t3_model,
        })
