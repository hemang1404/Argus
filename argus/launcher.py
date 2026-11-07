"""Argus Terminal Application Launcher.

Implements ADR-005:
- Dual-mode terminal launcher: in-place foreground mode vs dedicated Windows Terminal window.
- Supports native OpenTUI (Bun + Zig + SolidJS) frontend with graceful fallback to Python Textual.
- Locates Windows Terminal (wt.exe) with graceful Conhost (cmd.exe /c start) fallback.
- Desktop shortcut installer (.cmd and silent .vbs) for zero-console-flash launching.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


def find_windows_terminal() -> Optional[str]:
    """Locate Windows Terminal (wt.exe) across PATH and WindowsApps directory."""
    if sys.platform != "win32":
        return None

    # Check PATH first
    wt_in_path = shutil.which("wt.exe") or shutil.which("wt")
    if wt_in_path:
        return wt_in_path

    # Check WindowsApps standard installation location
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        candidate = Path(local_app_data) / "Microsoft" / "WindowsApps" / "wt.exe"
        if candidate.is_file():
            return str(candidate)

    return None


def find_opentui_entrypoint() -> Optional[tuple[str, Path]]:
    """Check if Bun and OpenTUI frontend are available."""
    bun_exe = shutil.which("bun") or shutil.which("bun.exe")
    if not bun_exe:
        return None

    repo_root = Path(__file__).resolve().parent.parent
    tui_dir = repo_root / "packages" / "tui"
    tui_index = tui_dir / "src" / "index.tsx"

    if tui_index.is_file():
        return bun_exe, tui_dir

    return None


def launch_terminal_app(
    dedicated: bool = False,
    workspace: str = ".",
) -> Optional[subprocess.Popen]:
    """Launch the Argus TUI in either the active terminal or a dedicated terminal window.

    Args:
        dedicated: If True, opens a separate standalone Windows Terminal window.
                   If False, runs directly inside the active terminal foreground.
        workspace: Path to the target workspace root directory.
    """
    workspace_path = str(Path(workspace).resolve())
    python_exe = sys.executable
    # OpenTUI (Bun frontend) is experimental; default to authoritative Python Textual TUI
    # unless explicitly requested via ARGUS_FRONTEND=opentui
    opentui_env = find_opentui_entrypoint() if os.environ.get("ARGUS_FRONTEND", "").lower() == "opentui" else None

    if not dedicated:
        # Run directly in foreground of current shell
        if opentui_env:
            bun_exe, tui_dir = opentui_env
            try:
                subprocess.run(
                    [bun_exe, "run", "src/index.tsx"],
                    cwd=str(tui_dir),
                )
                return None
            except Exception:
                pass  # Fallback to Python Textual on any execution error

        from argus.tui.app import ArgusTUIApp

        app = ArgusTUIApp(workspace=workspace_path)
        app.run()
        return None

    # Dedicated Window Mode
    wt_exe = find_windows_terminal()
    if wt_exe:
        if opentui_env:
            bun_exe, tui_dir = opentui_env
            cmd = [
                wt_exe,
                "-w", "new-window",
                "--title", "Argus — OpenCode TUI",
                "-d", str(tui_dir),
                bun_exe, "run", "src/index.tsx",
            ]
        else:
            cmd = [
                wt_exe,
                "-w", "new-window",
                "--title", "Argus — Code Recovery",
                "-d", workspace_path,
                python_exe, "-m", "argus.cli", "tui",
            ]
        flags = subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0
        return subprocess.Popen(cmd, creationflags=flags)

    # Fallback to Conhost start on Windows
    if sys.platform == "win32":
        if opentui_env:
            bun_exe, tui_dir = opentui_env
            cmd_str = f'cmd.exe /c start "Argus" /D "{tui_dir}" "{bun_exe}" run src/index.tsx'
        else:
            cmd_str = f'cmd.exe /c start "Argus" /D "{workspace_path}" "{python_exe}" -m argus.tui.app'
        return subprocess.Popen(cmd_str, shell=True)

    # Non-Windows dedicated fallback: run directly in foreground
    from argus.tui.app import ArgusTUIApp

    app = ArgusTUIApp(workspace=workspace_path)
    app.run()
    return None


def install_desktop_shortcut(target_dir: Optional[str | Path] = None) -> Path:
    """Create a Windows desktop launcher shortcut for Argus with zero console flash."""
    if target_dir:
        dest_dir = Path(target_dir).resolve()
    else:
        dest_dir = Path(os.path.expanduser("~/Desktop"))
        if not dest_dir.exists():
            dest_dir = Path.home()

    dest_dir.mkdir(parents=True, exist_ok=True)
    shortcut_path = dest_dir / "Argus.cmd"

    # Command script launcher
    script_content = (
        "@echo off\r\n"
        "title Argus Launcher\r\n"
        "argus app\r\n"
    )
    shortcut_path.write_text(script_content, encoding="utf-8")

    # Also generate a silent VBS launcher to prevent black console flash
    vbs_path = dest_dir / "Argus_Silent.vbs"
    vbs_content = (
        'Set WshShell = CreateObject("WScript.Shell")\r\n'
        'WshShell.Run "argus app", 0, False\r\n'
    )
    vbs_path.write_text(vbs_content, encoding="utf-8")

    return shortcut_path
