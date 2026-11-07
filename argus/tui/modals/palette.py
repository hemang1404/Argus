"""Searchable slash command palette modal inspired by Linear/Raycast."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option


@dataclass
class CommandEntry:
    """Definition of an authoritative slash command."""
    name: str
    aliases: List[str]
    description: str
    action_key: str


COMMAND_REGISTRY: List[CommandEntry] = [
    CommandEntry("/recover", ["/run", "/r", "/fix"], "Run test-gated code repair loop", "recover"),
    CommandEntry("/diff", ["/review", "/d", "/patch"], "Review and apply candidate diff", "show_diff"),
    CommandEntry("/policy", ["/p", "/budget", "/tier"], "Configure routing tier & budget", "config_policy"),
    CommandEntry("/model", ["/m", "/provider"], "Configure LLM provider & API key", "config_model"),
    CommandEntry("/connect", ["/c", "/mcp"], "Link local IDEs (Cursor, Claude, Aider)", "show_connect"),
    CommandEntry("/stats", ["/s", "/usage"], "View token spend & alpha savings", "show_stats"),
    CommandEntry("/doctor", ["/health"], "Run system diagnostics & health check", "show_doctor"),
    CommandEntry("/update", ["/upgrade"], "Check for and install updates", "show_update"),
    CommandEntry("/help", ["/h", "/?"], "Show keybindings & cheat sheet", "show_help"),
    CommandEntry("/clear", [], "Clear session routing stream history", "clear_stream"),
    CommandEntry("/quit", ["/q", "/exit"], "Exit Argus daemon", "quit_app"),
]


class CommandPaletteModal(ModalScreen[Optional[str]]):
    """Modal screen providing searchable slash commands with Linear/Raycast aesthetic."""

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Cancel"),
        Binding("down", "cursor_down", "Next", show=False),
        Binding("up", "cursor_up", "Previous", show=False),
    ]

    def __init__(self, initial_query: str = "") -> None:
        super().__init__()
        self.initial_query = initial_query
        self.filtered_commands: List[CommandEntry] = []

    def compose(self) -> ComposeResult:
        with Container(id="palette-container"):
            with Horizontal(id="palette-header"):
                yield Static("Commands", id="palette-title")
                yield Static("", id="palette-count")
            yield Input(
                value=self.initial_query,
                placeholder="Type a command…",
                id="palette-input",
            )
            yield OptionList(id="palette-options")
            yield Static("↑/↓ navigate   ·   enter select   ·   esc close", id="palette-footer")

    def on_mount(self) -> None:
        """Initialize command list on screen display."""
        input_widget = self.query_one("#palette-input", Input)
        input_widget.focus()
        self._filter_options(self.initial_query)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter command options in real time as the user types."""
        self._filter_options(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Select highlighted option on Enter."""
        options = self.query_one("#palette-options", OptionList)
        if options.highlighted is not None and 0 <= options.highlighted < len(self.filtered_commands):
            cmd = self.filtered_commands[options.highlighted]
            self.dismiss(cmd.action_key)
        elif self.filtered_commands:
            self.dismiss(self.filtered_commands[0].action_key)
        else:
            self.dismiss(None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Handle mouse click or selection on an option."""
        if 0 <= event.option_index < len(self.filtered_commands):
            cmd = self.filtered_commands[event.option_index]
            self.dismiss(cmd.action_key)

    def on_click(self, event: events.Click) -> None:
        """Handle first mouse click on any command option to execute it immediately."""
        try:
            options = self.query_one("#palette-options", OptionList)
        except Exception:
            return

        if event.widget == options or options in event.widget.ancestors_with_self:
            clicked_idx = event.style.meta.get("option")
            if clicked_idx is None and options.region.height > 0:
                rel_y = event.y - options.region.y
                if 0 <= rel_y < len(self.filtered_commands):
                    clicked_idx = rel_y
            if clicked_idx is not None and 0 <= clicked_idx < len(self.filtered_commands):
                cmd = self.filtered_commands[clicked_idx]
                self.dismiss(cmd.action_key)

    def action_dismiss_modal(self) -> None:
        """Dismiss without action."""
        self.dismiss(None)

    def action_cursor_down(self) -> None:
        options = self.query_one("#palette-options", OptionList)
        options.action_cursor_down()

    def action_cursor_up(self) -> None:
        options = self.query_one("#palette-options", OptionList)
        options.action_cursor_up()

    def _filter_options(self, query: str) -> None:
        """Populate OptionList with filtered command entries."""
        q = query.strip().lower()
        if q.startswith("/"):
            q_clean = q
        else:
            q_clean = "/" + q if q else ""

        self.filtered_commands = []
        options_widget = self.query_one("#palette-options", OptionList)
        options_widget.clear_options()

        for entry in COMMAND_REGISTRY:
            names = [entry.name] + entry.aliases
            match = False
            if not q or q == "/":
                match = True
            else:
                for n in names:
                    if q in n.lower() or (len(q) > 1 and q.lstrip("/") in n.lower()):
                        match = True
                        break
                if q_clean in entry.description.lower() or q.lstrip("/") in entry.description.lower():
                    match = True

            if match:
                self.filtered_commands.append(entry)
                cmd_col = f"{entry.name}".ljust(12)
                t = Text()
                t.append(cmd_col, style="bold #8188ff")
                t.append("  ", style="dim")
                t.append(entry.description, style="#9298a8")
                options_widget.add_option(Option(t, id=entry.action_key))

        self._update_count()

        if self.filtered_commands:
            options_widget.highlighted = 0

    def _update_count(self) -> None:
        """Show how many commands match the current query."""
        count = len(self.filtered_commands)
        label = "1 result" if count == 1 else f"{count} results"
        self.query_one("#palette-count", Static).update(label)
