"""Interactive Update Management Modal for Argus TUI."""

from __future__ import annotations

import asyncio
from typing import Optional

from rich.table import Table
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from argus import __version__
from argus.update import (
    InstallMethod,
    UpdateChannel,
    UpdateCheckResult,
    apply_update,
    check_for_updates,
    detect_install_method,
    verify_update,
)

# Design tokens matching styles.tcss
_TEXT = "#e1e4ec"
_SECONDARY = "#9298a8"
_MUTED = "#667085"
_BLUE = "#8188ff"
_GREEN = "#7bc79e"
_ORANGE = "#e88a13"
_RED = "#e07178"


class UpdateModal(ModalScreen[None]):
    """Modal dialog displaying version status, release channels, and upgrade actions."""

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.latest_result: Optional[UpdateCheckResult] = None
        self.is_updating: bool = False

    def compose(self) -> ComposeResult:
        with Container(id="update-container"):
            with Horizontal(classes="modal-header"):
                with Vertical(classes="modal-heading"):
                    yield Static("RELEASE", classes="kicker")
                    yield Static("Update manager", id="config-title", classes="modal-title")
                yield Static(f"[{_BLUE}]v{__version__}[/]", classes="modal-status")

            yield Static("Checking for updates...", id="update-content")

            with Horizontal(classes="modal-footer"):
                yield Static("", classes="footer-hint")
                yield Button("Check Now", id="btn-update-check", variant="default")
                yield Button("Update Now", id="btn-update-apply", variant="primary", disabled=True)
                yield Button("Close (Esc)", id="btn-update-close")

    def on_mount(self) -> None:
        """Run initial update check in the background."""
        self.query_one("#btn-update-close", Button).focus()
        self.run_worker(self._async_check_updates(), exclusive=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "btn-update-close":
            self.dismiss(None)
        elif button_id == "btn-update-check":
            self.run_worker(self._async_check_updates(), exclusive=True)
        elif button_id == "btn-update-apply":
            self.run_worker(self._async_apply_update(), exclusive=True)

    def action_dismiss_modal(self) -> None:
        if not self.is_updating:
            self.dismiss(None)

    async def _async_check_updates(self) -> None:
        """Query PyPI asynchronously and update the UI."""
        content_widget = self.query_one("#update-content", Static)
        apply_btn = self.query_one("#btn-update-apply", Button)
        check_btn = self.query_one("#btn-update-check", Button)

        check_btn.disabled = True
        apply_btn.disabled = True
        content_widget.update("[dim]Contacting PyPI to check for latest release...[/dim]")

        # Run synchronous network check in a thread
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, check_for_updates)
        self.latest_result = result
        check_btn.disabled = False

        self._render_status(result)

    def _render_status(self, result: UpdateCheckResult) -> None:
        content_widget = self.query_one("#update-content", Static)
        apply_btn = self.query_one("#btn-update-apply", Button)

        table = Table(show_header=True, header_style=f"bold {_BLUE}", expand=True, box=None, padding=(0, 1))
        table.add_column("Property", width=20, style=_MUTED)
        table.add_column("Value", style=_TEXT)

        table.add_row("Installed Version", f"v{result.current_version}")
        table.add_row("Latest Available", f"v{result.latest_version}")
        table.add_row("Release Channel", result.channel.value.capitalize())
        table.add_row("Install Method", result.install_method.value.upper())

        if result.error:
            table.add_row("Status", f"[bold {_RED}]Check Failed:[/] {result.error}")
            apply_btn.disabled = True
        elif result.is_source_checkout:
            table.add_row("Status", f"[{_ORANGE}]Source Checkout[/] (use git pull)")
            apply_btn.disabled = True
        elif result.has_update:
            table.add_row("Status", f"[bold {_GREEN}]Update Available (v{result.latest_version})[/]")
            apply_btn.disabled = False
        else:
            table.add_row("Status", f"[bold {_GREEN}]Argus is up to date ✓[/]")
            apply_btn.disabled = True

        content_widget.update(table)

    async def _async_apply_update(self) -> None:
        """Perform the upgrade and post-upgrade verification."""
        if not self.latest_result or not self.latest_result.has_update:
            return

        self.is_updating = True
        content_widget = self.query_one("#update-content", Static)
        apply_btn = self.query_one("#btn-update-apply", Button)
        check_btn = self.query_one("#btn-update-check", Button)

        apply_btn.disabled = True
        check_btn.disabled = True

        method = self.latest_result.install_method
        target_ver = self.latest_result.latest_version
        pre = (self.latest_result.channel == UpdateChannel.BETA)

        content_widget.update(
            f"[bold #818cf8]Upgrading argus-recovery to v{target_ver} via {method.value}...[/bold #818cf8]\n"
            "[dim]Please wait, this may take a moment.[/dim]"
        )

        loop = asyncio.get_running_loop()
        success, msg = await loop.run_in_executor(
            None,
            lambda: apply_update(method=method, target_version=target_ver, pre=pre),
        )

        if not success:
            self.is_updating = False
            check_btn.disabled = False
            content_widget.update(f"[bold #ef4444]Update failed:[/bold #ef4444]\n{msg}")
            return

        verified, v_msg = await loop.run_in_executor(
            None,
            lambda: verify_update(expected_version=target_ver),
        )

        self.is_updating = False
        check_btn.disabled = False

        if verified:
            content_widget.update(
                f"[bold #3fb950]✓ Successfully updated to v{target_ver}![/bold #3fb950]\n\n"
                "[bold #f8fafc]Please restart Argus to complete the update.[/bold #f8fafc]"
            )
        else:
            content_widget.update(
                f"[bold #3fb950]✓ Upgrade completed[/bold #3fb950]\n\n"
                f"[dim]CLI check response: {v_msg}[/dim]\n"
                "[bold #f8fafc]Please restart Argus to use the new version.[/bold #f8fafc]"
            )
