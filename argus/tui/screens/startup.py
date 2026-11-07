"""Argus startup screen adhering to the signature Linear/Raycast minimalist identity."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.containers import Container, Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Input, Static

from argus.config import ArgusConfig
from argus.tui.modals.palette import CommandPaletteModal
from argus.tui.widgets.status_bar import StatusBar
from argus.tui.widgets.wordmark import ArgusWordmark

# Executables that start a test invocation; everything after them is the command.
RUNNER_TOKENS = frozenset(
    {
        "pytest",
        "py.test",
        "unittest",
        "tox",
        "nox",
        "uv",
        "poetry",
        "pipenv",
        "npm",
        "npx",
        "pnpm",
        "yarn",
        "cargo",
        "go",
        "make",
        "dotnet",
        "mix",
    }
)


class StartupScreen(Screen[None]):
    """Signature minimalist developer landing cockpit."""

    BINDINGS = [
        Binding("tab", "open_policy", "Policy"),
        Binding("ctrl+k", "open_palette", "Palette"),
        Binding("ctrl+p", "open_palette", "Commands"),
        Binding("question_mark", "open_help", "Help", show=True),
        Binding("ctrl+c", "quit_app", "Exit", show=False),
        Binding("q", "quit_app", "Quit", show=False),
    ]

    def __init__(self, workspace: str = ".", config: Optional[ArgusConfig] = None) -> None:
        super().__init__()
        self.workspace = str(Path(workspace).resolve())
        self.config = config or ArgusConfig()
        self.stream_records: list = []

    def compose(self) -> ComposeResult:
        with Vertical(id="startup-outer"):
            with Vertical(id="startup-center-unit"):
                # 1. Center Emblem & Wordmark with verification tag
                with Vertical(id="startup-hero"):
                    yield ArgusWordmark(config=self.config, id="wordmark")
                    yield Static(
                        "/doctor to verify runtime v0.2.0",
                        id="descriptor",
                    )

                # 2. Signature Command Box (Image 2 style: Left accent bar, 2-row layout)
                with Vertical(id="command-box"):
                    with Horizontal(id="command-row-top"):
                        yield Static("❯", id="command-prompt-glyph")
                        yield Input(
                            placeholder='Run test command... "pytest tests/test_calculator.py" or "/" for commands...',
                            id="command-input",
                        )
                    with Horizontal(id="command-row-bottom"):
                        yield Static(
                            f"[dim #64748b]Recover[/dim #64748b]  [dim #64748b]·[/dim #64748b]  [bold #818cf8]argus/auto[/bold #818cf8]  [dim #64748b]·[/dim #64748b]  [#f8fafc]${self.config.budget_limit_usd:.2f} cap[/]",
                            id="sub-status",
                        )
                        yield Static(
                            "[#f8fafc]tab[/] [#64748b]policy[/]   [#f8fafc]ctrl+p[/] [#64748b]commands[/]",
                            id="key-hints",
                        )

                # 3. Tip row below command box (Image 2 style)
                yield Static(
                    "[#d97706]●[/] [bold #f8fafc]Tip[/] [#64748b]Run[/] [bold #818cf8]/connect[/] [#64748b]to link Cursor, Claude Code, or Aider[/]",
                    id="startup-tip",
                )

        yield StatusBar(workspace=self.workspace, version="v0.2.0")

    def on_mount(self) -> None:
        """Focus the command input on screen mount."""
        inp = self.query_one("#command-input", Input)
        inp.focus()
        self.update_status()

    def update_status(self) -> None:
        """Update the sub-status line in command box."""
        if self.is_mounted and self.query("#sub-status"):
            tier_name = getattr(self.app, "current_tier", "argus/auto")
            self.query_one("#sub-status", Static).update(
                f"[dim #64748b]Recover[/dim #64748b]  [dim #64748b]·[/dim #64748b]  [bold #818cf8]{tier_name}[/bold #818cf8]  [dim #64748b]·[/dim #64748b]  [#f8fafc]${self.config.budget_limit_usd:.2f} cap[/]"
            )

    def on_input_changed(self, event: Input.Changed) -> None:
        """If user types '/' as the first character, trigger the command palette."""
        val = event.value
        if val == "/":
            event.input.value = ""
            self.action_open_palette(initial_query="/")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key in command box."""
        val = event.value.strip()
        event.input.value = ""

        if not val:
            return

        if val.startswith("/"):
            parts = val.lstrip("/").split(maxsplit=1)
            cmd = parts[0].lower() if parts else ""
            args_str = parts[1].strip() if len(parts) > 1 else ""

            if cmd in ("policy", "p", "budget", "tier", "attempts"):
                self.app.open_policy_modal()
            elif cmd in ("recover", "run", "r", "fix"):
                target_file = ""
                test_cmd = ""
                if args_str:
                    first_token = args_str.split()[0].strip("\"'")
                    runner = first_token.replace("\\", "/").rsplit("/", 1)[-1].lower()
                    if runner in RUNNER_TOKENS or runner.startswith("python"):
                        # A full runner invocation is the test command; never
                        # reinterpret its arguments as the target file.
                        test_cmd = args_str
                    else:
                        tokens = args_str.split()
                        for tok in tokens:
                            clean = tok.strip("\"'")
                            if clean.endswith(".py") and "test" not in clean.lower():
                                target_file = clean
                                break
                        if target_file and target_file in args_str:
                            test_cmd = args_str.replace(target_file, "").strip()
                        else:
                            test_cmd = args_str

                self.app.start_recovery_workflow(target_file=target_file, test_command=test_cmd)
            elif cmd in ("diff", "review", "d", "patch"):
                run_id = args_str if args_str else None
                self.app.show_diff_screen(run_id=run_id)
            elif cmd in ("model", "m", "provider"):
                self.app._open_config_modal("model")
            elif cmd in ("connect", "c", "mcp"):
                self.app.open_connect_modal()
            elif cmd in ("stats", "s", "usage"):
                self.app.open_stats_modal()
            elif cmd in ("doctor", "health"):
                self.app.open_doctor_modal()
            elif cmd in ("help", "h", "?"):
                self.app.open_help_modal()
            elif cmd in ("clear",):
                self.stream_records = []
                self.notify("Stream cleared.", severity="information")
            elif cmd in ("quit", "q", "exit"):
                self.app.exit()
            else:
                self.notify(f"Unknown command: /{cmd}. Press '/' or Ctrl+K to browse.", severity="warning")
        else:
            # Direct test command entered -> launch recovery workflow with prefilled test
            self.app.start_recovery_workflow(target_file="", test_command=val)

    def on_click(self, event: events.Click) -> None:
        """Allow clicking on sub-status, key hints, or tip to trigger action directly."""
        try:
            hints = self.query_one("#key-hints")
            if event.widget == hints or hints in event.widget.ancestors_with_self:
                rel_x = event.x - hints.region.x
                if rel_x < (hints.region.width // 2):
                    self.action_open_policy()
                else:
                    self.action_open_palette()
                return
        except NoMatches:
            pass

        try:
            sub = self.query_one("#sub-status")
            if event.widget == sub or sub in event.widget.ancestors_with_self:
                self.action_open_policy()
                return
        except NoMatches:
            pass

        try:
            tip = self.query_one("#startup-tip")
            if event.widget == tip or tip in event.widget.ancestors_with_self:
                self.app.open_connect_modal()
                return
        except NoMatches:
            pass

    def action_open_palette(self, initial_query: str = "") -> None:
        """Trigger the command palette modal."""
        self.app.open_command_palette(initial_query=initial_query)

    def action_open_policy(self) -> None:
        """Trigger the routing policy modal."""
        self.app.open_policy_modal()

    def action_open_help(self) -> None:
        """Open the Help & cheatsheet modal."""
        self.app.open_help_modal()

    def action_quit_app(self) -> None:
        """Exit the application cleanly."""
        self.app.exit()
