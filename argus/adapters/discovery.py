"""Client Discovery and Zero-Friction Auto-Connection Module.

Automatically discovers external coding agents and IDEs installed on the machine:
- Antigravity IDE & CLI
- OpenCode CLI & Desktop
- Gemini CLI
- Cursor IDE
- Claude Code CLI
- Aider CLI
- Continue (VS Code & JetBrains)

Provides one-action connection so users never need to manually write JSON files
or memorize terminal flags.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from argus.adapters.presets import export_preset, get_preset
from argus.mcp.installer import (
    get_default_paths,
    install_antigravity,
    install_gemini,
    install_opencode,
)
from argus.providers.antigravity import find_antigravity_cli


@dataclass
class DiscoveredClient:
    """Status record of a detected coding tool on the local machine."""
    key: str
    display_name: str
    installed: bool
    configured: bool
    detection_reason: str
    config_summary: str
    mcp_supported: bool = True
    gateway_supported: bool = True


def discover_clients(workspace_root: Path | str = ".") -> List[DiscoveredClient]:
    """Scan the local system and workspace to detect all supported coding environments."""
    ws = Path(workspace_root).resolve()
    home = Path.home()
    appdata = Path(os.getenv("APPDATA")) if os.getenv("APPDATA") else home / "AppData" / "Roaming"
    localappdata = Path(os.getenv("LOCALAPPDATA")) if os.getenv("LOCALAPPDATA") else home / "AppData" / "Local"

    results: List[DiscoveredClient] = []

    # 1. Antigravity IDE & CLI
    agy_cli = find_antigravity_cli()
    agy_cfg = get_default_paths()["antigravity"]["global"]
    agy_ws_gw = ws / ".agents" / "argus-gateway.json"
    agy_has_mcp = False
    if agy_cfg.is_file():
        try:
            data = json.loads(agy_cfg.read_text(encoding="utf-8"))
            agy_has_mcp = "argus" in data.get("mcpServers", {})
        except Exception:
            pass

    agy_installed = bool(agy_cli) or (home / ".gemini").is_dir()
    agy_configured = agy_has_mcp or agy_ws_gw.is_file()
    agy_reason = f"Detected CLI at {agy_cli.name}" if agy_cli else ("Found ~/.gemini profile" if (home / ".gemini").is_dir() else "Not detected")
    results.append(
        DiscoveredClient(
            key="antigravity",
            display_name="Antigravity IDE & CLI",
            installed=agy_installed,
            configured=agy_configured,
            detection_reason=agy_reason,
            config_summary="MCP in ~/.gemini/config, Gateway in .agents/",
            mcp_supported=True,
            gateway_supported=True,
        )
    )

    # 2. OpenCode CLI & Desktop
    oc_cli = shutil.which("opencode") or shutil.which("opencode.exe")
    oc_dir = appdata / "opencode" if sys.platform == "win32" else home / ".config" / "opencode"
    oc_global_file = oc_dir / "opencode.json"
    oc_ws_file = ws / "opencode.json"
    oc_has_mcp = False
    if oc_global_file.is_file():
        try:
            data = json.loads(oc_global_file.read_text(encoding="utf-8"))
            oc_has_mcp = "argus" in data.get("mcpServers", {})
        except Exception:
            pass

    oc_installed = bool(oc_cli) or oc_dir.is_dir() or oc_ws_file.is_file()
    oc_configured = oc_has_mcp or oc_ws_file.is_file()
    oc_reason = "Found 'opencode' in PATH" if oc_cli else (f"Found {oc_dir}" if oc_dir.is_dir() else "Not detected")
    results.append(
        DiscoveredClient(
            key="opencode",
            display_name="OpenCode",
            installed=oc_installed,
            configured=oc_configured,
            detection_reason=oc_reason,
            config_summary="MCP in opencode.json, Gateway in workspace",
            mcp_supported=True,
            gateway_supported=True,
        )
    )

    # 3. Cursor IDE
    cursor_cli = shutil.which("cursor") or shutil.which("cursor.cmd")
    cursor_dir = localappdata / "Programs" / "cursor" if sys.platform == "win32" else home / ".cursor"
    cursor_rules = ws / ".cursorrules"
    cursor_installed = bool(cursor_cli) or cursor_dir.is_dir() or (appdata / "Cursor").is_dir()
    cursor_configured = cursor_rules.is_file()
    cursor_reason = "Found Cursor installation" if cursor_installed else "Not detected"
    results.append(
        DiscoveredClient(
            key="cursor",
            display_name="Cursor IDE",
            installed=cursor_installed,
            configured=cursor_configured,
            detection_reason=cursor_reason,
            config_summary=".cursorrules in workspace",
            mcp_supported=True,
            gateway_supported=True,
        )
    )

    # 4. Claude Code CLI
    claude_cli = shutil.which("claude") or shutil.which("claude.cmd")
    claude_env = ws / ".env.claude"
    claude_installed = bool(claude_cli) or (home / ".claude.json").is_file()
    claude_configured = claude_env.is_file()
    claude_reason = "Found 'claude' CLI" if claude_cli else ("Found ~/.claude.json" if (home / ".claude.json").is_file() else "Not detected")
    results.append(
        DiscoveredClient(
            key="claude",
            display_name="Claude Code CLI",
            installed=claude_installed,
            configured=claude_configured,
            detection_reason=claude_reason,
            config_summary=".env.claude (Messages API adapter)",
            mcp_supported=False,
            gateway_supported=True,
        )
    )

    # 5. Aider CLI
    aider_cli = shutil.which("aider") or shutil.which("aider.exe")
    aider_conf = ws / ".aider.conf.yml"
    aider_installed = bool(aider_cli) or (home / ".aider.conf.yml").is_file()
    aider_configured = aider_conf.is_file()
    aider_reason = "Found 'aider' in PATH" if aider_cli else "Not detected"
    results.append(
        DiscoveredClient(
            key="aider",
            display_name="Aider CLI",
            installed=aider_installed,
            configured=aider_configured,
            detection_reason=aider_reason,
            config_summary=".aider.conf.yml in workspace",
            mcp_supported=False,
            gateway_supported=True,
        )
    )

    # 6. Continue (VS Code / JetBrains)
    continue_global = home / ".continue" / "config.json"
    continue_ws = ws / ".continue" / "config.json"
    continue_installed = continue_global.is_file() or (home / ".continue").is_dir()
    continue_configured = False
    if continue_global.is_file():
        try:
            content = continue_global.read_text(encoding="utf-8")
            continue_configured = "argus" in content.lower()
        except Exception:
            pass
    if continue_ws.is_file():
        continue_configured = True

    continue_reason = "Found ~/.continue configuration" if continue_installed else "Not detected"
    results.append(
        DiscoveredClient(
            key="continue",
            display_name="Continue Extension",
            installed=continue_installed,
            configured=continue_configured,
            detection_reason=continue_reason,
            config_summary=".continue/config.json",
            mcp_supported=False,
            gateway_supported=True,
        )
    )

    # 7. Gemini CLI
    gemini_cli = shutil.which("gemini")
    gemini_global = home / ".gemini" / "settings.json"
    gemini_installed = bool(gemini_cli) or gemini_global.is_file()
    gemini_has_mcp = False
    if gemini_global.is_file():
        try:
            data = json.loads(gemini_global.read_text(encoding="utf-8"))
            gemini_has_mcp = "argus" in data.get("mcpServers", {})
        except Exception:
            pass
    gemini_configured = gemini_has_mcp
    gemini_reason = "Found Gemini settings" if gemini_installed else "Not detected"
    results.append(
        DiscoveredClient(
            key="gemini",
            display_name="Gemini CLI",
            installed=gemini_installed,
            configured=gemini_configured,
            detection_reason=gemini_reason,
            config_summary="~/.gemini/settings.json",
            mcp_supported=True,
            gateway_supported=False,
        )
    )

    return results


def connect_client(key: str, workspace_root: Path | str = ".") -> Tuple[bool, str]:
    """Connect a specific client by generating its MCP configuration and/or gateway preset."""
    ws = Path(workspace_root).resolve()
    key_norm = key.lower().strip()

    try:
        if key_norm == "antigravity":
            # 1. Install MCP into global config
            install_antigravity(global_mode=True, workspace_root=ws)
            # 2. Export workspace gateway preset
            export_preset("antigravity", target_dir=ws, overwrite=True)
            return True, "Antigravity connected (MCP registered in ~/.gemini & .agents/argus-gateway.json created)."

        elif key_norm == "opencode":
            # 1. Install MCP into OpenCode config
            install_opencode(global_mode=True, workspace_root=ws)
            # 2. Export workspace gateway preset
            export_preset("opencode", target_dir=ws, overwrite=True)
            return True, "OpenCode connected (MCP registered & opencode.json created)."

        elif key_norm == "gemini":
            install_gemini(global_mode=True, workspace_root=ws)
            return True, "Gemini CLI connected (MCP registered in ~/.gemini/settings.json)."

        elif key_norm in ("cursor", "aider", "claude", "continue"):
            export_preset(key_norm, target_dir=ws, overwrite=True)
            preset = get_preset(key_norm)
            name = preset.display_name if preset else key_norm
            fname = preset.filename if preset else "config"
            return True, f"{name} connected (Created {fname} in workspace)."

        else:
            return False, f"Unknown client '{key}'. Available: antigravity, opencode, cursor, claude, aider, continue, gemini."

    except Exception as ex:
        return False, f"Failed to connect '{key}': {ex}"


def connect_all_detected(workspace_root: Path | str = ".") -> Dict[str, Tuple[bool, str]]:
    """Auto-connect every detected or installed client on the machine in one operation."""
    ws = Path(workspace_root).resolve()
    clients = discover_clients(ws)
    results: Dict[str, Tuple[bool, str]] = {}

    for client in clients:
        if client.installed:
            ok, msg = connect_client(client.key, workspace_root=ws)
            results[client.key] = (ok, msg)

    return results
