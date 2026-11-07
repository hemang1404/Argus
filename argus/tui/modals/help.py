"""Keyboard shortcuts and navigation cheat-sheet modal for Argus TUI."""

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


class HelpModal(ModalScreen[None]):
    """Clean terminal cheat-sheet displaying all keybindings and commands."""

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close"),
        Binding("enter", "dismiss_modal", "Close"),
        Binding("question_mark", "dismiss_modal", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Container(id="help-container"):
            with Horizontal(classes="modal-header"):
                with Vertical(classes="modal-heading"):
                    yield Static("SHORTCUTS", classes="kicker")
                    yield Static("Keyboard & commands", id="config-title", classes="modal-title")
                yield Static(f"[{_MUTED}]cheatsheet[/]", classes="modal-status")

            yield Static(self._build_table(), id="help-table")

            with Horizontal(classes="modal-footer"):
                yield Static(f"[{_MUTED}]? toggle cheatsheet[/]", classes="footer-hint")
                yield Button("Close (Esc)", variant="primary", id="btn-help-close")

    def on_mount(self) -> None:
        self.query_one("#btn-help-close", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def _build_table(self) -> Table:
        table = Table(show_header=True, expand=True, box=None, padding=(0, 1), header_style=f"bold {_BLUE}")
        table.add_column("Key / Command", style=f"bold {_TEXT}", width=22)
        table.add_column("Action & Scope", style=_MUTED)

        # Global
        table.add_row(f"[bold {_BLUE}]?[/]", "Toggle this help & shortcut cheatsheet (Global)")
        table.add_row(f"[bold {_BLUE}]Tab[/] / [bold {_BLUE}]Ctrl+P[/]", "Open slash-command palette modal")
        table.add_row(f"[bold {_BLUE}]/[/]", "Quick-filter commands from home prompt")
        table.add_row(f"[bold {_BLUE}]Esc[/]", "Back / Dismiss active modal or screen")
        table.add_row(f"[bold {_BLUE}]Ctrl+C[/] / [bold {_BLUE}]q[/]", "Cleanly exit Argus router daemon")

        # Execution
        table.add_row("", "")
        table.add_row(f"[bold {_GREEN}]z[/]", "Toggle 100% full-width zoom on patch synthesis pane (Execution)")
        table.add_row(f"[bold {_GREEN}]d[/]", "Jump directly to candidate diff review upon completion")

        # Diff Review
        table.add_row("", "")
        table.add_row(f"[bold {_ORANGE}]Enter[/] / [bold {_ORANGE}]a[/]", "Atomically apply verified patch to disk (Diff Review)")
        table.add_row(f"[bold {_ORANGE}]Esc[/] / [bold {_ORANGE}]d[/]", "Discard candidate patch and return")

        # Slash Commands
        table.add_row("", "")
        table.add_row(f"[bold {_BLUE}]/recover <cmd>[/]", "Directly run test-gated repair loop without modal form")
        table.add_row(f"[bold {_BLUE}]/diff[/]", "Review and apply verified candidate diff")
        table.add_row(f"[bold {_BLUE}]/policy[/]", "Configure dynamic routing tier & budget ceiling")
        table.add_row(f"[bold {_BLUE}]/model[/]", "Select inference model & configure API keys securely")
        table.add_row(f"[bold {_BLUE}]/connect[/]", "Auto-detect and link Cursor, Claude Code, and Aider")
        table.add_row(f"[bold {_BLUE}]/stats[/]", "View token spend & cost savings vs frontier baseline")
        table.add_row(f"[bold {_BLUE}]/doctor[/]", "Run system diagnostics & health checks")
        table.add_row(f"[bold {_BLUE}]/update[/]", "Check for and install updates to Argus")

        return table

