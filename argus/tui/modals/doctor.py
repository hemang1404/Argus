"""System diagnostics and doctor modal dialog."""

from __future__ import annotations

import shutil
import sys

from rich.table import Table
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from argus.config import ArgusConfig
from argus.providers.storage import ProviderStorage


# Design tokens matching styles.tcss and other modals
_TEXT = "#e1e4ec"
_SECONDARY = "#9298a8"
_MUTED = "#667085"
_BLUE = "#8188ff"
_GREEN = "#7bc79e"
_ORANGE = "#e88a13"
_RED = "#e07178"


class DoctorModal(ModalScreen[None]):
    """Modal displaying environment diagnostics and provider connectivity."""

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close"),
        Binding("enter", "dismiss_modal", "Close"),
    ]

    def __init__(self, config: ArgusConfig) -> None:
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        with Container(id="doctor-container"):
            with Horizontal(classes="modal-header"):
                with Vertical(classes="modal-heading"):
                    yield Static("SYSTEM", classes="kicker")
                    yield Static("System diagnostics", id="doctor-title", classes="modal-title")
                yield Static(f"[{_GREEN}]● health check[/]", classes="modal-status")

            yield Static("Running diagnostics...", id="doctor-content")

            with Horizontal(classes="modal-footer"):
                yield Static("", classes="footer-hint")
                yield Button("Close (Esc)", variant="primary", id="doctor-close")

    def on_mount(self) -> None:
        """Execute diagnostic checks and update table."""
        self._run_diagnostics()
        self.query_one("#doctor-close", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def _run_diagnostics(self) -> None:
        """Run checks and render formatted Rich Table."""
        table = Table(show_header=True, header_style=f"bold {_BLUE}", expand=True, box=None, padding=(0, 1))
        table.add_column("Component", width=20, style=_MUTED)
        table.add_column("Status", width=14)
        table.add_column("Details", style=_TEXT)

        # 1. Python
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        if sys.version_info >= (3, 11):
            table.add_row("Python", f"[bold {_GREEN}]PASS[/]", f"Python {py_ver}")
        else:
            table.add_row("Python", f"[bold {_RED}]FAIL[/]", f"Python {py_ver} (< 3.11)")

        # 2. Git
        git_path = shutil.which("git")
        if git_path:
            table.add_row("Git", f"[bold {_GREEN}]PASS[/]", git_path)
        else:
            table.add_row("Git", f"[bold {_ORANGE}]WARN[/]", "Git not found in PATH")

        # 3. Pytest
        pytest_path = shutil.which("pytest")
        if pytest_path:
            table.add_row("Pytest", f"[bold {_GREEN}]PASS[/]", pytest_path)
        else:
            table.add_row("Pytest", f"[bold {_ORANGE}]WARN[/]", "Pytest not found in PATH")

        # 4. Host runtime (binary discovery only: nothing is executed or authenticated here)
        from argus.providers.antigravity import find_antigravity_cli
        cli_path = find_antigravity_cli()
        if cli_path:
            table.add_row("Antigravity CLI", f"[bold {_BLUE}]BINARY FOUND[/]", f"{cli_path} (not an authenticated check)")
        else:
            table.add_row("Antigravity CLI", f"[{_MUTED}]NOT FOUND[/]", "Direct API / local gateway only")

        # 5. Active provider: only what is actually configured, never inferred from the host binary.
        # The key itself may live in the saved key store (system keyring or env) when the
        # provider was set up through the settings screen or policy file, which leaves
        # provider_config empty, so fall back to the key store before reporting no key.
        provider_cfg = getattr(self.config, "provider_config", None)
        active_provider = self.config.provider or (provider_cfg.name if provider_cfg else None)
        if not active_provider:
            table.add_row("Active Provider", f"[bold {_ORANGE}]SETUP NEEDED[/]", "Run 'argus provider set'")
        else:
            has_key = bool(getattr(provider_cfg, "api_key", None) or self.config.deepseek_api_key)
            if not has_key:
                try:
                    has_key = bool(ProviderStorage().get_api_key(active_provider))
                except Exception:
                    has_key = False
            key_note = "key stored" if has_key else "no stored key (host/local provider or key missing)"
            table.add_row(
                "Active Provider",
                f"[bold {_BLUE}]CONFIGURED[/]",
                f"{active_provider} ({key_note}); connectivity not tested: run 'argus provider test'",
            )

        # 6. Active model
        active_model = self.config.model or "(provider default)"
        table.add_row("Active Model", f"[bold {_BLUE}]SET[/]", active_model)

        self.query_one("#doctor-content", Static).update(table)
