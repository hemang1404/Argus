"""Tests for Terminal Application Launcher & Asset Serving."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

from argus.api.server import create_app
from argus.launcher import (
    find_windows_terminal,
    install_desktop_shortcut,
)


def test_find_windows_terminal():
    """Verify that Windows Terminal detection executes safely."""
    wt = find_windows_terminal()
    if sys.platform == "win32":
        # On Windows, if wt is found, verify it exists on disk
        if wt is not None:
            assert isinstance(wt, str)
            assert Path(wt).exists()
    else:
        assert wt is None


def test_install_desktop_shortcut_in_dir(tmp_path: Path):
    """Verify desktop shortcut installer generates both .cmd and silent .vbs."""
    shortcut = install_desktop_shortcut(target_dir=tmp_path)
    assert shortcut.exists()
    assert shortcut.name == "Argus.cmd"
    cmd_content = shortcut.read_text(encoding="utf-8")
    assert "argus app" in cmd_content

    vbs_path = tmp_path / "Argus_Silent.vbs"
    assert vbs_path.exists()
    vbs_content = vbs_path.read_text(encoding="utf-8")
    assert "argus app" in vbs_content


def test_ui_static_asset_serving():
    """Verify that create_app mounts and serves index.html and CSS/JS."""
    app = create_app(workspace=".")
    client = TestClient(app)

    # 1. Root serves index.html
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Argus" in resp.text
    assert "Task Composer" in resp.text
    assert "Execution Waterfall" in resp.text

    # 2. CSS is served
    resp_css = client.get("/app.css")
    assert resp_css.status_code == 200
    assert "--bg-primary" in resp_css.text

    # 3. JS is served
    resp_js = client.get("/app.js")
    assert resp_js.status_code == 200
    assert "initSession" in resp_js.text


def test_dedicated_conhost_fallback_never_uses_a_shell(monkeypatch, tmp_path: Path):
    """The Windows fallback must spawn an argv list with shell=False, even for hostile paths."""
    import argus.launcher as launcher

    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return object()

    hostile = tmp_path / "a & calc ^x"
    hostile.mkdir()
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setattr(launcher, "find_windows_terminal", lambda: None)
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)

    launcher.launch_terminal_app(dedicated=True, workspace=str(hostile))

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert isinstance(argv, list)
    assert not kwargs.get("shell", False)
    assert "cmd.exe" not in [a.lower() for a in argv]
    assert argv[:3] == [sys.executable, "-m", "argus.tui.app"]
    assert kwargs["cwd"] == str(hostile.resolve())
