"""Host Integrations Installer for Antigravity, OpenCode, and Gemini CLI.

Safely configures local MCP server entrypoints, preserves existing configuration,
creates dated backups, and provides handshake verification.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def get_default_paths() -> Dict[str, Dict[str, Path]]:
    """Determine user-level and workspace-level configuration paths for supported hosts."""
    home = Path.home()
    appdata = Path(os.getenv("APPDATA")) if os.getenv("APPDATA") else home / "AppData" / "Roaming"

    return {
        "antigravity": {
            "global": home / ".gemini" / "config" / "mcp_config.json",
            "workspace": Path(".gemini") / "mcp_config.json",
        },
        "opencode": {
            "global": appdata / "opencode" / "opencode.json" if sys.platform == "win32" else home / ".config" / "opencode" / "opencode.json",
            "workspace": Path(".opencode") / "opencode.json",
        },
        "gemini": {
            "global": home / ".gemini" / "settings.json",
            "workspace": Path(".gemini") / "settings.json",
        },
    }


def backup_and_modify_json(
    config_file: Path,
    modifier_fn: callable,
) -> Tuple[Path, Optional[Path]]:
    """Create a timestamped backup before modifying a JSON configuration file."""
    config_file.parent.mkdir(parents=True, exist_ok=True)
    backup_file: Optional[Path] = None

    data: Dict[str, Any] = {}
    if config_file.is_file():
        # Create backup
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_file = config_file.with_name(f"{config_file.name}.bak.{timestamp}")
        shutil.copy2(config_file, backup_file)
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    modified_data = modifier_fn(data)
    config_file.write_text(json.dumps(modified_data, indent=2), encoding="utf-8")
    return config_file, backup_file


def _build_argus_mcp_entry(python_exe: Optional[str] = None) -> Dict[str, Any]:
    """Construct standard MCP server entry."""
    exe = python_exe or sys.executable
    return {
        "command": exe,
        "args": ["-m", "argus.mcp"],
    }


def install_antigravity(
    global_mode: bool = True,
    workspace_root: Optional[Path] = None,
    custom_path: Optional[Path] = None,
    python_exe: Optional[str] = None,
) -> Tuple[Path, Optional[Path]]:
    """Install or update Argus MCP server configuration in Antigravity."""
    if custom_path:
        target_file = custom_path
    elif global_mode:
        target_file = get_default_paths()["antigravity"]["global"]
    else:
        root = workspace_root or Path.cwd()
        target_file = root / ".gemini" / "mcp_config.json"

    def update(data: Dict[str, Any]) -> Dict[str, Any]:
        if "mcpServers" not in data or not isinstance(data["mcpServers"], dict):
            data["mcpServers"] = {}
        data["mcpServers"]["argus"] = _build_argus_mcp_entry(python_exe)
        return data

    return backup_and_modify_json(target_file, update)


def install_opencode(
    global_mode: bool = True,
    workspace_root: Optional[Path] = None,
    custom_path: Optional[Path] = None,
    python_exe: Optional[str] = None,
) -> Tuple[Path, Optional[Path]]:
    """Install or update Argus MCP server configuration in OpenCode."""
    if custom_path:
        target_file = custom_path
    elif global_mode:
        target_file = get_default_paths()["opencode"]["global"]
    else:
        root = workspace_root or Path.cwd()
        target_file = root / "opencode.json"

    def update(data: Dict[str, Any]) -> Dict[str, Any]:
        if "mcpServers" not in data or not isinstance(data["mcpServers"], dict):
            data["mcpServers"] = {}
        data["mcpServers"]["argus"] = _build_argus_mcp_entry(python_exe)
        return data

    return backup_and_modify_json(target_file, update)


def install_gemini(
    global_mode: bool = True,
    workspace_root: Optional[Path] = None,
    custom_path: Optional[Path] = None,
    python_exe: Optional[str] = None,
) -> Tuple[Path, Optional[Path]]:
    """Install or update Argus MCP server configuration in Gemini CLI."""
    if custom_path:
        target_file = custom_path
    elif global_mode:
        target_file = get_default_paths()["gemini"]["global"]
    else:
        root = workspace_root or Path.cwd()
        target_file = root / ".gemini" / "settings.json"

    def update(data: Dict[str, Any]) -> Dict[str, Any]:
        if "mcpServers" not in data or not isinstance(data["mcpServers"], dict):
            data["mcpServers"] = {}
        data["mcpServers"]["argus"] = _build_argus_mcp_entry(python_exe)
        return data

    return backup_and_modify_json(target_file, update)


def install_all(
    global_mode: bool = True,
    workspace_root: Optional[Path] = None,
) -> Dict[str, Tuple[Path, Optional[Path]]]:
    """Install Argus MCP configuration across all supported hosts."""
    return {
        "antigravity": install_antigravity(global_mode=global_mode, workspace_root=workspace_root),
        "opencode": install_opencode(global_mode=global_mode, workspace_root=workspace_root),
        "gemini": install_gemini(global_mode=global_mode, workspace_root=workspace_root),
    }


def verify_mcp_handshake(timeout_seconds: float = 8.0) -> Dict[str, Any]:
    """Test start the MCP server, complete initialization handshake, list tools, and call argus_status."""
    cmd = [sys.executable, "-m", "argus.mcp"]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        # Step 1: Send 'initialize' request
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "argus-installer-probe", "version": "1.0"},
            },
        }
        proc.stdin.write(json.dumps(init_req) + "\n")
        proc.stdin.flush()

        init_resp_line = proc.stdout.readline()
        if not init_resp_line:
            stderr_out = proc.stderr.read()
            return {"success": False, "stage": "initialize", "error": f"No response from server. Stderr: {stderr_out}"}

        init_resp = json.loads(init_resp_line)
        server_name = init_resp.get("result", {}).get("serverInfo", {}).get("name")
        if server_name != "argus":
            return {"success": False, "stage": "initialize", "error": f"Unexpected server name: {server_name}"}

        # Step 2: Send initialized notification
        init_notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        proc.stdin.write(json.dumps(init_notif) + "\n")
        proc.stdin.flush()

        # Step 3: Send 'tools/list' request
        tools_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        proc.stdin.write(json.dumps(tools_req) + "\n")
        proc.stdin.flush()

        tools_resp_line = proc.stdout.readline()
        tools_resp = json.loads(tools_resp_line)
        tools = [t["name"] for t in tools_resp.get("result", {}).get("tools", [])]

        required_tools = {"argus_status", "argus_recover", "argus_get_candidate", "argus_apply_candidate"}
        missing = required_tools - set(tools)
        if missing:
            return {"success": False, "stage": "tools_list", "error": f"Missing expected tools: {missing}"}

        # Step 4: Send 'tools/call' for 'argus_status'
        call_req = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "argus_status",
                "arguments": {},
            },
        }
        proc.stdin.write(json.dumps(call_req) + "\n")
        proc.stdin.flush()

        call_resp_line = proc.stdout.readline()
        call_resp = json.loads(call_resp_line)
        content_items = call_resp.get("result", {}).get("content", [])
        if not content_items:
            return {"success": False, "stage": "tools_call", "error": "Empty tool response content"}

        status_text = content_items[0].get("text", "")
        parsed_status = json.loads(status_text)
        if not parsed_status.get("runtime_ready"):
            return {"success": False, "stage": "tools_call", "error": "argus_status reported runtime_ready=False"}

        return {
            "success": True,
            "server_name": server_name,
            "tools_found": tools,
            "active_provider": parsed_status.get("active_provider", {}).get("name"),
            "message": "MCP handshake, tool discovery, and tool call verified successfully.",
        }

    except Exception as ex:
        return {"success": False, "stage": "exception", "error": str(ex)}
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
