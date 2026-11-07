"""Tests for Client Auto-Discovery and One-Click Setup (argus setup and ConnectModal)."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from argus.adapters.discovery import (
    DiscoveredClient,
    connect_all_detected,
    connect_client,
    discover_clients,
)
from argus.tui.modals.connect import ConnectModal


def test_discover_clients_structure(tmp_path: Path):
    """Verify discover_clients returns structured records for all supported tools."""
    clients = discover_clients(workspace_root=tmp_path)
    assert len(clients) >= 6

    keys = {c.key for c in clients}
    assert "antigravity" in keys
    assert "opencode" in keys
    assert "cursor" in keys
    assert "claude" in keys
    assert "gemini" in keys

    for c in clients:
        assert isinstance(c, DiscoveredClient)
        assert isinstance(c.installed, bool)
        assert isinstance(c.configured, bool)
        assert len(c.display_name) > 0


def test_connect_client_antigravity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify connect_client('antigravity') writes .agents/argus-gateway.json."""
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    ok, msg = connect_client("antigravity", workspace_root=tmp_path)
    assert ok is True
    assert "Antigravity connected" in msg

    gw_file = tmp_path / ".agents" / "argus-gateway.json"
    assert gw_file.is_file()
    data = json.loads(gw_file.read_text(encoding="utf-8"))
    assert data["name"] == "Argus Local Gateway"
    assert "argus/auto" in data["models"]["default"]


def test_connect_client_opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify connect_client('opencode') creates workspace opencode.json."""
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    ok, msg = connect_client("opencode", workspace_root=tmp_path)
    assert ok is True
    assert "OpenCode connected" in msg

    oc_file = tmp_path / "opencode.json"
    assert oc_file.is_file()
    data = json.loads(oc_file.read_text(encoding="utf-8"))
    assert "argus" in data["providers"]
    assert data["defaultModel"] == "argus/auto"


def test_connect_client_cursor(tmp_path: Path):
    """Verify connect_client('cursor') creates .cursorrules."""
    ok, msg = connect_client("cursor", workspace_root=tmp_path)
    assert ok is True
    assert (tmp_path / ".cursorrules").is_file()


def test_connect_modal_table_mount(tmp_path: Path):
    """Verify ConnectModal builds discovery table cleanly."""
    modal = ConnectModal(workspace=tmp_path)
    table = modal._build_discovery_table()
    assert len(table.rows) >= 6
