"""Guided recovery workflow screen for collecting and validating repair inputs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from rich.cells import cell_len
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Input, Static

from argus.config import ArgusConfig
from argus.tui.widgets.status_bar import StatusBar

# #workflow-card is 74 cells wide with 2 cells of horizontal padding on each side.
CONTEXT_LINE_MAX_WIDTH = 70

# Escalation qualifiers that add length without changing which model it is.
_MODEL_SUFFIX_RE = re.compile(r"-(?:low|medium|high|thinking)$")


def _compact_model(model: str, limit: int = 18) -> str:
    """Shorten a model id so the runtime summary fits on one row."""
    short = _MODEL_SUFFIX_RE.sub("", model)
    return short if len(short) <= limit else short[: limit - 1] + "…"


class RecoveryWorkflowScreen(Screen[None]):
    """Guided screen collecting parameters for a non-destructive recovery loop in clean OpenCode layout."""

    BINDINGS = [
        Binding("escape", "cancel_workflow", "Back"),
    ]

    def __init__(
        self,
        workspace: str,
        config: ArgusConfig,
        initial_target: str = "",
        initial_test_cmd: str = "",
    ) -> None:
        super().__init__()
        self.workspace = workspace
        self.config = config
        self.initial_target = initial_target
        self.initial_test_cmd = initial_test_cmd

    def compose(self) -> ComposeResult:
        with Vertical(id="workflow-outer"):
            with Container(id="workflow-card"):
                with Horizontal(id="workflow-header-row"):
                    yield Static("Recovery runtime", id="workflow-card-title")
                    yield Static("[#667085]esc back[/]", id="workflow-esc")

                yield Static(
                    "Non-destructive patch synthesis in an isolated git worktree.",
                    id="workflow-card-subtitle",
                )

                with Horizontal(classes="workflow-field-label"):
                    yield Static("Test command")
                    yield Static("[#9298a8]required[/]", classes="required")
                yield Input(
                    value=self.initial_test_cmd,
                    placeholder="pytest tests/test_calculator.py",
                    id="input-test-cmd",
                    classes="workflow-input",
                )

                with Horizontal(classes="workflow-field-label"):
                    yield Static("Target file")
                    yield Static("[#667085]optional · auto-localized when blank[/]", classes="required")
                yield Input(
                    value=self.initial_target,
                    placeholder="src/module.py",
                    id="input-target-file",
                    classes="workflow-input",
                )

                yield Static("", id="error-banner")
                yield Static(self._format_context_text(), id="context-info")
                yield Static(
                    # Exactly CONTEXT_LINE_MAX_WIDTH cells: the card clips wider lines.
                    "[#7bc79e]●[/] Isolated worktree · no writes before approval · verified output only",
                    id="workflow-guardrail",
                )

                with Horizontal(id="workflow-actions-bar"):
                    yield Button("Cancel (Esc)", id="btn-cancel")
                    yield Button("Run Recovery (Enter)", variant="primary", id="btn-run")

        yield StatusBar(workspace=self.workspace, version="v0.2.0")

    def on_mount(self) -> None:
        """Focus input and initialize context view."""
        if not self.initial_test_cmd:
            self.query_one("#input-test-cmd", Input).focus()
        else:
            self.query_one("#input-target-file", Input).focus()
        self._update_context()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Update context and clear error banner on change."""
        self.query_one("#error-banner", Static).update("")
        self._update_context()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.action_cancel_workflow()
        elif event.button.id == "btn-run":
            self._validate_and_submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Submit on Enter."""
        self._validate_and_submit()

    def action_cancel_workflow(self) -> None:
        """Return to startup screen."""
        self.app.pop_screen()

    def _format_context_text(self) -> Text:
        """Render the runtime summary as a single line that never wraps in the card."""
        tier_name = getattr(self.app, "current_tier", "argus/auto")
        display_model = self.config.model
        if not display_model:
            try:
                from argus.providers.discovery import resolve_dynamic_tiers
                display_model = resolve_dynamic_tiers()["t1"]["model"]
            except Exception:
                display_model = "gemini-2.5-flash"

        def build(model: str) -> Text:
            line = Text()
            line.append("Runtime ", style="#667085")
            line.append(f"{tier_name}", style="bold #8188ff")
            if model:
                line.append(f" ({_compact_model(model)})", style="#667085")
            line.append(" · Budget ", style="#667085")
            line.append(f"${self.config.budget_limit_usd:.2f}", style="bold #e1e4ec")
            line.append(" · Attempts ", style="#667085")
            line.append(f"{self.config.max_attempts}", style="bold #e1e4ec")
            return line

        line = build(display_model or "")
        if display_model and cell_len(line.plain) > CONTEXT_LINE_MAX_WIDTH:
            # Drop the optional model label rather than wrapping 'Attempts' onto its
            # own centred row (the budget/attempts are the load-bearing values).
            line = build("")
        return line

    def _update_context(self) -> None:
        """Update context pane dynamically."""
        if not self.is_mounted:
            return
        info = self.query_one("#context-info", Static)
        info.update(self._format_context_text())

    def _validate_and_submit(self) -> None:
        """Validate inputs against workspace safety rules and launch execution."""
        test_cmd = self.query_one("#input-test-cmd", Input).value.strip()
        target_val = self.query_one("#input-target-file", Input).value.strip()
        err_banner = self.query_one("#error-banner", Static)

        if not test_cmd:
            err_banner.update("Error: Test command is required.")
            self.query_one("#input-test-cmd", Input).focus()
            return

        resolved_target: Optional[str] = None
        if target_val:
            target_path = Path(target_val)
            if not target_path.is_absolute():
                target_path = Path(self.workspace) / target_path

            if not target_path.exists():
                err_banner.update(f"Error: File '{target_val}' does not exist on disk.")
                return

            if not target_path.is_file():
                err_banner.update(f"Error: Path '{target_val}' is a directory, not a file.")
                return

            # Workspace containment check
            try:
                target_path.resolve().relative_to(Path(self.workspace).resolve())
            except ValueError:
                err_banner.update("Security error: Target file is outside workspace root.")
                return

            resolved_target = str(target_path.resolve())

        # Dispatch execution to App controller
        self.app.launch_recovery(
            target_file=resolved_target,
            test_cmd=test_cmd,
            test_file=None,
        )
