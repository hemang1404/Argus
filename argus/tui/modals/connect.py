"""Modal dialog for one-click client auto-discovery and connection."""

from __future__ import annotations

from pathlib import Path
from typing import List

from rich import box
from rich.table import Table
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from argus.adapters.discovery import DiscoveredClient, connect_all_detected, connect_client, discover_clients

_TEXT = "#e1e4ec"
_SECONDARY = "#9298a8"
_MUTED = "#667085"
_BLUE = "#8188ff"
_GREEN = "#7bc79e"
_ORANGE = "#e88a13"


class ConnectModal(ModalScreen[None]):
    """Modal displaying detected local coding tools with one-click zero-friction connection."""

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close"),
        Binding("enter", "dismiss_modal", "Close"),
        Binding("c", "action_connect_all", "Connect All"),
    ]

    def __init__(self, workspace: str | Path = ".") -> None:
        super().__init__()
        self.workspace = Path(workspace).resolve()

    def compose(self) -> ComposeResult:
        clients = discover_clients(self.workspace)
        with Container(id="connect-container"):
            with Horizontal(classes="modal-header"):
                with Vertical(classes="modal-heading"):
                    yield Static("CLIENTS", classes="kicker")
                    yield Static("Client integrations", classes="modal-title")
                yield Static(self._status_chip(clients), classes="modal-status")

            yield Static(
                "Argus auto-detects installed coding agents and IDEs. Connect them here — "
                "no terminal configuration or JSON editing required.",
                classes="modal-subtitle",
            )

            yield Static(self._build_summary(clients), id="connect-summary")
            yield Static(self._build_discovery_table(clients), id="connect-table")
            yield Static("", id="connect-status-msg")

            with Horizontal(classes="modal-footer"):
                yield Static("c connect all available", classes="footer-hint")
                yield Button("Close (Esc)", id="btn-connect-close")
                yield Button("Connect All (c)", variant="primary", id="btn-connect-all")

    def on_mount(self) -> None:
        self.query_one("#btn-connect-all", Button).focus()

    # ---- presentation ----------------------------------------------------
    @staticmethod
    def _counts(clients: List[DiscoveredClient]) -> tuple[int, int, int]:
        connected = sum(1 for c in clients if c.configured)
        ready = sum(1 for c in clients if c.installed and not c.configured)
        missing = sum(1 for c in clients if not c.installed)
        return connected, ready, missing

    def _status_chip(self, clients: Optional[List[DiscoveredClient]] = None) -> str:
        if clients is None:
            clients = discover_clients(self.workspace)
        connected, ready, _ = self._counts(clients)
        if connected:
            return f"[{_GREEN}]● {connected} connected[/]"
        if ready:
            return f"[{_ORANGE}]● {ready} ready[/]"
        return f"[{_MUTED}]○ nothing detected[/]"

    def _build_summary(self, clients: Optional[List[DiscoveredClient]] = None) -> str:
        if clients is None:
            clients = discover_clients(self.workspace)
        connected, ready, missing = self._counts(clients)
        return (
            f"[{_GREEN}]● {connected} connected[/]"
            f"   [{_ORANGE}]● {ready} ready[/]"
            f"   [{_MUTED}]○ {missing} not detected[/]"
        )

    @staticmethod
    def _layers(c: DiscoveredClient) -> str:
        layers = []
        if c.mcp_supported:
            layers.append("MCP")
        if c.gateway_supported:
            layers.append("gateway")
        return " + ".join(layers) or "—"

    @staticmethod
    def _status_cell(c: DiscoveredClient) -> str:
        if c.configured:
            return f"[bold {_GREEN}]● Connected[/]"
        if c.installed:
            return f"[bold {_ORANGE}]● Ready to connect[/]"
        return f"[{_MUTED}]○ Not detected[/]"

    def _build_discovery_table(self, clients: Optional[List[DiscoveredClient]] = None) -> Table:
        if clients is None:
            clients = discover_clients(self.workspace)
        table = Table(
            show_header=True,
            expand=True,
            box=box.SIMPLE_HEAD,
            header_style=f"bold {_MUTED}",
            border_style="#1c1f27",
            padding=(0, 1),
        )
        table.add_column("Client / tool", width=24)
        table.add_column("System status", width=17)
        table.add_column("Layers", style=_MUTED)

        groups = [
            ("Connected", [c for c in clients if c.configured], _GREEN),
            ("Ready to connect", [c for c in clients if c.installed and not c.configured], _ORANGE),
            ("Not detected", [c for c in clients if not c.installed], _MUTED),
        ]
        for label, members, color in groups:
            if not members:
                continue
            table.add_row(f"[bold {color}]{label.upper()}[/] [{_MUTED}]{len(members)}[/]", "", "")
            for c in members:
                name_style = f"bold {_TEXT}" if (c.configured or c.installed) else _MUTED
                table.add_row(
                    f"[{name_style}]{c.display_name}[/]",
                    self._status_cell(c),
                    self._layers(c),
                )

        if not clients:
            table.add_row(f"[{_MUTED}]No coding tools detected on this machine.[/]", "", "")
        return table

    def _refresh_view(self) -> None:
        clients = discover_clients(self.workspace)
        self.query_one("#connect-table", Static).update(self._build_discovery_table(clients))
        self.query_one("#connect-summary", Static).update(self._build_summary(clients))

    def _report_connection_result(self, results: dict) -> None:
        connected_names = [k for k, (ok, _) in results.items() if ok]
        msg_widget = self.query_one("#connect-status-msg", Static)
        if connected_names:
            msg_widget.update(
                f"[bold {_GREEN}]✓ Connected: {', '.join(connected_names)}[/]\n"
                f"[{_MUTED}]Gateway config written and MCP registered. Ready to route requests.[/]"
            )
        else:
            msg_widget.update(f"[{_ORANGE}]All detected tools are already connected.[/]")

    # ---- events ----------------------------------------------------------
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-connect-close":
            self.dismiss(None)
        elif event.button.id == "btn-connect-all":
            results = connect_all_detected(self.workspace)
            self._refresh_view()
            self._report_connection_result(results)

    def action_connect_all(self) -> None:
        """Trigger connecting all detected tools."""
        results = connect_all_detected(self.workspace)
        self._refresh_view()
        self._report_connection_result(results)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)
