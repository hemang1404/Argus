"""Unit tests for Argus update service, CLI update command, and TUI update modal."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from argus import __version__
from argus.cli import main
from argus.update import (
    DEFAULT_PYPI_JSON_URL,
    InstallMethod,
    UpdateChannel,
    UpdateCheckResult,
    apply_update,
    check_for_updates,
    detect_install_method,
    get_update_command,
    verify_update,
)


# =============================================================================
# 1. Installer Detection Tests
# =============================================================================

def test_detect_install_method_source(monkeypatch):
    """When inside a git checkout with pyproject.toml, reports SOURCE."""
    # Running directly within the Argus git repository
    method = detect_install_method()
    assert method == InstallMethod.SOURCE


def test_detect_install_method_uv(monkeypatch):
    """Detects UV when UV_TOOL_DIR is set or in prefix."""
    with patch("argus.update.Path.is_dir", return_value=False):
        monkeypatch.setenv("UV_TOOL_DIR", "C:\\Users\\test\\.local\\share\\uv\\tools")
        monkeypatch.setattr(sys, "prefix", "C:\\Users\\test\\.local\\share\\uv\\tools\\argus-recovery")
        assert detect_install_method() == InstallMethod.UV


def test_detect_install_method_pipx(monkeypatch):
    """Detects PIPX when PIPX_HOME or prefix contains pipx."""
    with patch("argus.update.Path.is_dir", return_value=False):
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.setenv("PIPX_HOME", "C:\\Users\\test\\.local\\pipx")
        monkeypatch.setattr(sys, "prefix", "C:\\Users\\test\\.local\\pipx\\venvs\\argus-recovery")
        assert detect_install_method() == InstallMethod.PIPX


def test_detect_install_method_pip(monkeypatch):
    """Detects PIP when inside standard venv."""
    with patch("argus.update.Path.is_dir", return_value=False):
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.delenv("PIPX_HOME", raising=False)
        monkeypatch.setattr(sys, "prefix", "C:\\test\\venv")
        monkeypatch.setattr(sys, "base_prefix", "C:\\Python314")
        assert detect_install_method() == InstallMethod.PIP


# =============================================================================
# 2. Command Generation Tests (Security & Integrity)
# =============================================================================

def test_get_update_command_uv():
    cmd = get_update_command(InstallMethod.UV)
    assert isinstance(cmd, list)
    assert "uv" in cmd[0]
    assert cmd[1:4] == ["tool", "upgrade", "argus-recovery"]

    cmd_pre = get_update_command(InstallMethod.UV, pre=True)
    assert "--prerelease" in cmd_pre

    cmd_pin = get_update_command(InstallMethod.UV, target_version="0.3.0")
    assert "argus-recovery==0.3.0" in cmd_pin[-1]


def test_get_update_command_pipx():
    cmd = get_update_command(InstallMethod.PIPX)
    assert isinstance(cmd, list)
    assert "pipx" in cmd[0]
    assert cmd[1:3] == ["upgrade", "argus-recovery"]

    cmd_pre = get_update_command(InstallMethod.PIPX, pre=True)
    assert "--pip-args=--pre" in cmd_pre

    cmd_pin = get_update_command(InstallMethod.PIPX, target_version="0.3.0")
    assert "argus-recovery==0.3.0" in cmd_pin[-1]


def test_get_update_command_pip():
    cmd = get_update_command(InstallMethod.PIP)
    assert isinstance(cmd, list)
    assert cmd[0] == sys.executable
    assert cmd[1:5] == ["-m", "pip", "install", "--upgrade"]
    assert cmd[5] == "argus-recovery"

    cmd_pre = get_update_command(InstallMethod.PIP, pre=True)
    assert "--pre" in cmd_pre

    cmd_pin = get_update_command(InstallMethod.PIP, target_version="0.3.0")
    assert "argus-recovery==0.3.0" in cmd_pin[-1]


def test_get_update_command_unsupported():
    with pytest.raises(ValueError, match="not supported"):
        get_update_command(InstallMethod.SOURCE)


# =============================================================================
# 3. Update Checking & PEP 440 Comparisons (Mocked PyPI)
# =============================================================================

def test_check_for_updates_available():
    mock_payload = {
        "info": {"version": "0.3.0"},
        "releases": {
            "0.1.0": [{}],
            "0.2.0": [{}],
            "0.3.0": [{}],
        },
    }

    mock_client = MagicMock(spec=httpx.Client)
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_payload
    mock_client.get.return_value = mock_resp

    res = check_for_updates(channel=UpdateChannel.STABLE, client=mock_client)
    assert res.error is None
    assert res.has_update is True
    assert res.latest_version == "0.3.0"


def test_check_for_updates_up_to_date():
    mock_payload = {
        "info": {"version": __version__},
        "releases": {
            __version__: [{}],
        },
    }

    mock_client = MagicMock(spec=httpx.Client)
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_payload
    mock_client.get.return_value = mock_resp

    res = check_for_updates(channel=UpdateChannel.STABLE, client=mock_client)
    assert res.error is None
    assert res.has_update is False
    assert res.latest_version == __version__


def test_check_for_updates_prerelease_filtering():
    # If stable channel is queried, ignore beta versions
    mock_payload = {
        "info": {"version": "0.3.0b1"},
        "releases": {
            "0.1.0": [{}],
            "0.2.0b1": [{}],
            "0.3.0b1": [{}],
        },
    }

    mock_client = MagicMock(spec=httpx.Client)
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_payload
    mock_client.get.return_value = mock_resp

    # Without include_prereleases and STABLE channel:
    res = check_for_updates(channel=UpdateChannel.STABLE, include_prereleases=False, client=mock_client)
    assert res.latest_version == "0.1.0"

    # With BETA channel:
    res_beta = check_for_updates(channel=UpdateChannel.BETA, include_prereleases=True, client=mock_client)
    assert res_beta.latest_version == "0.3.0b1"


def test_check_for_updates_network_error():
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.get.side_effect = httpx.ConnectError("Network unreachable")

    res = check_for_updates(client=mock_client)
    assert res.has_update is False
    assert res.error is not None
    assert "Network error" in res.error


def test_check_for_updates_http_error():
    mock_client = MagicMock(spec=httpx.Client)
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 503
    mock_client.get.return_value = mock_resp

    res = check_for_updates(client=mock_client)
    assert res.has_update is False
    assert "HTTP 503" in res.error


# =============================================================================
# 4. Apply & Verify Tests (Mocked Subprocess)
# =============================================================================

def test_apply_update_source_rejection():
    # Attempting automated update when source checkout is detected must fail safely
    success, msg = apply_update(method=InstallMethod.SOURCE)
    assert success is False
    assert "source git checkout" in msg


def test_apply_update_success():
    def mock_runner(cmd):
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="Successfully installed", stderr="")

    success, msg = apply_update(method=InstallMethod.PIP, target_version="0.3.0", runner=mock_runner)
    assert success is True
    assert "Successfully installed" in msg


def test_apply_update_failure():
    def mock_runner(cmd):
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="Permission denied")

    success, msg = apply_update(method=InstallMethod.PIP, target_version="0.3.0", runner=mock_runner)
    assert success is False
    assert "Permission denied" in msg


def test_verify_update():
    def mock_runner(cmd):
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="argus, version 0.3.0\n", stderr="")

    verified, msg = verify_update(expected_version="0.3.0", runner=mock_runner)
    assert verified is True

    verified_mismatch, err_msg = verify_update(expected_version="0.4.0", runner=mock_runner)
    assert verified_mismatch is False
    assert "Version mismatch" in err_msg


# =============================================================================
# 5. CLI Command Tests
# =============================================================================

def test_cli_update_check_flag():
    runner = CliRunner()
    fake_result = UpdateCheckResult(
        current_version="0.2.0b1",
        latest_version="0.3.0",
        has_update=True,
        channel=UpdateChannel.STABLE,
        install_method=InstallMethod.PIP,
    )

    with patch("argus.update.check_for_updates", return_value=fake_result):
        res = runner.invoke(main, ["update", "--check"])
        assert res.exit_code == 0
        assert "Argus Update Manager" in res.output
        assert "v0.2.0b1" in res.output
        assert "v0.3.0" in res.output
        assert "Run argus update to install this version." in res.output


def test_cli_update_check_up_to_date():
    runner = CliRunner()
    fake_result = UpdateCheckResult(
        current_version="0.2.0b1",
        latest_version="0.2.0b1",
        has_update=False,
        channel=UpdateChannel.STABLE,
        install_method=InstallMethod.PIP,
    )

    with patch("argus.update.check_for_updates", return_value=fake_result):
        res = runner.invoke(main, ["update", "--check"])
        assert res.exit_code == 0
        assert "Argus is already up to date." in res.output


def test_cli_update_source_checkout_refusal():
    runner = CliRunner()
    fake_result = UpdateCheckResult(
        current_version="0.2.0b1",
        latest_version="0.3.0",
        has_update=True,
        channel=UpdateChannel.STABLE,
        install_method=InstallMethod.SOURCE,
        is_source_checkout=True,
    )

    with patch("argus.update.check_for_updates", return_value=fake_result):
        res = runner.invoke(main, ["update"])
        assert res.exit_code == 1
        assert "Refusing automated update" in res.output


def test_cli_update_apply_with_yes():
    runner = CliRunner()
    fake_result = UpdateCheckResult(
        current_version="0.2.0b1",
        latest_version="0.3.0",
        has_update=True,
        channel=UpdateChannel.STABLE,
        install_method=InstallMethod.PIP,
        is_source_checkout=False,
    )

    with patch("argus.update.check_for_updates", return_value=fake_result), \
         patch("argus.update.apply_update", return_value=(True, "Updated successfully")), \
         patch("argus.update.verify_update", return_value=(True, "argus, version 0.3.0")):
        res = runner.invoke(main, ["update", "--yes"])
        assert res.exit_code == 0
        assert "Successfully upgraded to Argus v0.3.0!" in res.output


# =============================================================================
# 6. TUI UpdateModal Interaction Tests
# =============================================================================

@pytest.mark.asyncio
async def test_tui_update_modal_render():
    try:
        from textual.app import App
        from argus.tui.modals.update import UpdateModal
    except ImportError:
        pytest.skip("textual not installed in this environment")

    modal = UpdateModal()
    assert modal is not None
    assert modal.latest_result is None


@pytest.mark.asyncio
async def test_tui_update_modal_check_and_apply_flow():
    """Verify UpdateModal renders available update and runs apply with restart message."""
    try:
        from textual.app import App, ComposeResult
        from textual.widgets import Button, Static
        from argus.tui.modals.update import UpdateModal
    except ImportError:
        pytest.skip("textual not installed in this environment")

    fake_result = UpdateCheckResult(
        current_version="0.2.0b1",
        latest_version="0.3.0",
        has_update=True,
        channel=UpdateChannel.STABLE,
        install_method=InstallMethod.PIP,
        is_source_checkout=False,
    )

    class TestApp(App[None]):
        def compose(self) -> ComposeResult:
            yield Static("Base App")

    with patch("argus.tui.modals.update.check_for_updates", return_value=fake_result), \
         patch("argus.tui.modals.update.apply_update", return_value=(True, "Upgraded successfully")), \
         patch("argus.tui.modals.update.verify_update", return_value=(True, "argus, version 0.3.0")):

        app = TestApp()
        async with app.run_test(size=(100, 30)) as pilot:
            modal = UpdateModal()
            await app.push_screen(modal)
            await pilot.pause()

            # Verify that update check completed and enabled the upgrade button
            apply_btn = modal.query_one("#btn-update-apply", Button)
            assert apply_btn.disabled is False

            # Trigger update apply
            await modal._async_apply_update()
            await pilot.pause()

            content = str(modal.query_one("#update-content", Static).content)
            assert "Successfully updated to v0.3.0" in content
            assert "restart Argus" in content


@pytest.mark.asyncio
async def test_tui_update_modal_apply_failure():
    """Verify UpdateModal renders error when apply_update fails."""
    try:
        from textual.app import App, ComposeResult
        from textual.widgets import Button, Static
        from argus.tui.modals.update import UpdateModal
    except ImportError:
        pytest.skip("textual not installed in this environment")

    fake_result = UpdateCheckResult(
        current_version="0.2.0b1",
        latest_version="0.3.0",
        has_update=True,
        channel=UpdateChannel.STABLE,
        install_method=InstallMethod.PIP,
        is_source_checkout=False,
    )

    class TestApp(App[None]):
        def compose(self) -> ComposeResult:
            yield Static("Base App")

    with patch("argus.tui.modals.update.check_for_updates", return_value=fake_result), \
         patch("argus.tui.modals.update.apply_update", return_value=(False, "Permission denied write access")):

        app = TestApp()
        async with app.run_test(size=(100, 30)) as pilot:
            modal = UpdateModal()
            await app.push_screen(modal)
            await pilot.pause()

            await modal._async_apply_update()
            await pilot.pause()

            content = str(modal.query_one("#update-content", Static).content)
            assert "Update failed" in content
            assert "Permission denied" in content


@pytest.mark.asyncio
async def test_tui_update_modal_dismiss_blocked_during_active_update():
    """Verify that modal dismissal is blocked while an update operation is in flight."""
    try:
        from argus.tui.modals.update import UpdateModal
    except ImportError:
        pytest.skip("textual not installed in this environment")

    modal = UpdateModal()
    modal.is_updating = True

    with patch.object(modal, "dismiss") as mock_dismiss:
        modal.action_dismiss_modal()
        mock_dismiss.assert_not_called()

    modal.is_updating = False
    with patch.object(modal, "dismiss") as mock_dismiss:
        modal.action_dismiss_modal()
        mock_dismiss.assert_called_once_with(None)
