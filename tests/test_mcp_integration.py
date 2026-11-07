"""Tests for Argus MCP Server, Tool Execution, and Host Installers."""

import json
import tempfile
from pathlib import Path
import pytest

from argus.mcp.installer import (
    install_antigravity,
    install_opencode,
    install_gemini,
    verify_mcp_handshake,
)
from argus.mcp.server import create_mcp_server
from argus.runtime.candidate import CandidateArtifactStore


def test_host_installers_and_backups():
    """Verify that installers update mcpServers, preserve existing keys, and create backups."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        # 1. Antigravity installation with existing configuration
        anti_cfg = tmp_path / "antigravity.json"
        anti_cfg.write_text(json.dumps({
            "existingPlugin": "custom-v1",
            "mcpServers": {
                "other_tool": {"command": "node", "args": ["server.js"]}
            }
        }, indent=2), encoding="utf-8")

        cfg_file, bak_file = install_antigravity(custom_path=anti_cfg, python_exe="python-custom")
        assert cfg_file.is_file()
        assert bak_file is not None and bak_file.is_file()

        # Check modified file
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert data["existingPlugin"] == "custom-v1"
        assert "other_tool" in data["mcpServers"]
        assert "argus" in data["mcpServers"]
        assert data["mcpServers"]["argus"]["command"] == "python-custom"
        assert data["mcpServers"]["argus"]["args"] == ["-m", "argus.mcp"]

        # Check backup file preserved original
        bak_data = json.loads(bak_file.read_text(encoding="utf-8"))
        assert "argus" not in bak_data["mcpServers"]

        # 2. OpenCode installation (fresh file)
        opencode_cfg = tmp_path / "opencode.json"
        cfg_file, bak_file = install_opencode(custom_path=opencode_cfg)
        assert cfg_file.is_file()
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert "argus" in data["mcpServers"]

        # 3. Gemini CLI installation (fresh file)
        gemini_cfg = tmp_path / "settings.json"
        cfg_file, bak_file = install_gemini(custom_path=gemini_cfg)
        assert cfg_file.is_file()
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert "argus" in data["mcpServers"]


def test_mcp_direct_tools():
    """Verify FastMCP tools directly."""
    server = create_mcp_server()
    # FastMCP tools can be directly called as python functions through server.get_tool or callable
    tools = {t.name: t for t in server._tool_manager.list_tools()}
    assert "argus_status" in tools
    assert "argus_recover" in tools
    assert "argus_get_candidate" in tools
    assert "argus_apply_candidate" in tools


def test_mcp_stdio_handshake_and_call():
    """Verify live MCP stdio initialization handshake, tool listing, and argus_status call."""
    res = verify_mcp_handshake(timeout_seconds=10.0)
    assert res["success"] is True, f"MCP handshake failed at stage '{res.get('stage')}': {res.get('error')}"
    assert res["server_name"] == "argus"
    assert "argus_status" in res["tools_found"]
    assert "argus_recover" in res["tools_found"]
    assert "argus_get_candidate" in res["tools_found"]
    assert "argus_apply_candidate" in res["tools_found"]
