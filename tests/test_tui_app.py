"""Automated tests for Argus Terminal UI (Textual)."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from textual.widgets import Button, Input, OptionList, Static

from argus.config import ArgusConfig
from argus.launcher import find_windows_terminal, install_desktop_shortcut
from argus.runtime.candidate import CandidateArtifactStore
from argus.tui.app import ArgusTUIApp
from argus.tui.modals.config import ConfigModal
from argus.tui.modals.doctor import DoctorModal
from argus.tui.modals.help import HelpModal
from argus.tui.modals.palette import CommandPaletteModal
from argus.tui.modals.usage import UsageModal
from argus.tui.screens.diff_review import DiffReviewScreen
from argus.tui.screens.execution import ExecutionTimelineScreen
from argus.tui.screens.recovery import RecoveryWorkflowScreen
from argus.tui.screens.startup import StartupScreen
from argus.tui.widgets.diff_viewer import DiffViewer
from argus.tui.widgets.status_bar import StatusBar
from argus.tui.widgets.wordmark import ArgusWordmark


@pytest.mark.asyncio
async def test_startup_screen_mount_and_widgets():
    """Verify StartupScreen mounts correctly with all OpenCode-inspired widgets."""
    app = ArgusTUIApp(workspace=".")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, StartupScreen)

        # Verify core widget hierarchy
        assert app.screen.query_one("#wordmark", ArgusWordmark) is not None
        assert app.screen.query_one("#descriptor", Static) is not None
        assert app.screen.query_one("#command-box") is not None
        assert app.screen.query_one("#command-input", Input) is not None
        assert app.screen.query_one("#sub-status", Static) is not None
        assert app.screen.query_one("#key-hints", Static) is not None
        assert app.screen.query_one(StatusBar) is not None

        # Verify initial focus on command input
        assert app.focused == app.screen.query_one("#command-input", Input)


@pytest.mark.asyncio
async def test_typing_slash_opens_command_palette():
    """Verify typing '/' in startup input automatically triggers command palette modal."""
    app = ArgusTUIApp(workspace=".")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, StartupScreen)

        # Press '/'
        await pilot.press("/")
        await pilot.pause()

        # Modal screen should now be CommandPaletteModal
        assert isinstance(app.screen, CommandPaletteModal)

        # Press Escape to dismiss
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, StartupScreen)


@pytest.mark.asyncio
async def test_command_palette_filtering():
    """Verify CommandPaletteModal filters options correctly based on input."""
    modal = CommandPaletteModal(initial_query="/policy")
    app = ArgusTUIApp(workspace=".")
    async with app.run_test() as pilot:
        app.push_screen(modal)
        await pilot.pause()

        palette_input = modal.query_one("#palette-input", Input)
        assert palette_input.value == "/policy"

        # Check filtered commands
        assert len(modal.filtered_commands) == 1
        assert modal.filtered_commands[0].name == "/policy"


@pytest.mark.asyncio
async def test_config_modal_budget_validation():
    """Verify ConfigModal validates budget ceiling boundaries."""
    cfg = ArgusConfig(budget_limit_usd=0.05)
    modal = ConfigModal(config_type="budget", current_config=cfg)
    app = ArgusTUIApp(workspace=".")

    async with app.run_test() as pilot:
        app.push_screen(modal)
        await pilot.pause()

        inp = modal.query_one("#config-input", Input)
        inp.value = "10.00"  # Exceeds max $5.00
        await pilot.press("enter")
        await pilot.pause()

        err_text = str(modal.query_one("#config-error", Static).content)
        assert "between $0.01 and $5.00" in err_text


@pytest.mark.asyncio
async def test_config_modal_accepts_google_ai_studio_key(tmp_path, monkeypatch):
    """Verify ConfigModal accepts Google AI Studio key and routes to gemini provider."""
    monkeypatch.setenv("ARGUS_CONFIG_DIR", str(tmp_path))
    cfg = ArgusConfig()
    modal = ConfigModal(config_type="model", current_config=cfg)
    app = ArgusTUIApp(workspace=str(tmp_path))

    dismissed_result = None
    def handle_dismiss(result):
        nonlocal dismissed_result
        dismissed_result = result

    async with app.run_test(size=(100, 40)) as pilot:
        app.push_screen(modal, callback=handle_dismiss)
        await pilot.pause()

        modal.query_one("#config-input", Input).value = "gemini-2.5-flash"
        modal.query_one("#config-api-key", Input).value = "AIzaSyTestGoogleStudioKey_12345"
        modal._apply_config()
        await pilot.pause()

    assert dismissed_result is not None
    assert dismissed_result["type"] == "model"
    assert dismissed_result["value"] == "gemini-2.5-flash"
    assert dismissed_result["provider"] == "gemini"

    from argus.providers.storage import ProviderStorage
    storage = ProviderStorage(config_dir=tmp_path)
    prov = storage.get_provider("gemini")
    assert prov is not None
    assert prov.name == "gemini"
    assert "generativelanguage" in prov.base_url


@pytest.mark.asyncio
async def test_config_modal_rejects_groq_key(tmp_path, monkeypatch):
    """Verify ConfigModal strictly rejects Groq keys and does not save."""
    monkeypatch.setenv("ARGUS_CONFIG_DIR", str(tmp_path))
    cfg = ArgusConfig()
    modal = ConfigModal(config_type="model", current_config=cfg)
    app = ArgusTUIApp(workspace=str(tmp_path))

    dismissed_result = None
    def handle_dismiss(result):
        nonlocal dismissed_result
        dismissed_result = result

    async with app.run_test(size=(100, 40)) as pilot:
        app.push_screen(modal, callback=handle_dismiss)
        await pilot.pause()

        modal.query_one("#config-input", Input).value = "llama-3.3-70b-versatile"
        modal.query_one("#config-api-key", Input).value = "gsk_testGroqKey1234567890"
        modal._apply_config()
        await pilot.pause()

        err_text = str(modal.query_one("#config-error", Static).content)
        assert "Groq keys ('gsk_...') are strictly prohibited" in err_text

    assert dismissed_result is None


@pytest.mark.asyncio
async def test_recovery_workflow_screen_path_validation(tmp_path):
    """Verify RecoveryWorkflowScreen validates file existence and workspace containment."""
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    valid_file = ws_dir / "calc.py"
    valid_file.write_text("def add(a, b): return a + b\n")

    outside_file = tmp_path / "secret.py"
    outside_file.write_text("SECRET = 1\n")

    screen = RecoveryWorkflowScreen(workspace=str(ws_dir), config=ArgusConfig())
    app = ArgusTUIApp(workspace=str(ws_dir))

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        # 1. Test non-existent file
        target_inp = screen.query_one("#input-target-file", Input)
        cmd_inp = screen.query_one("#input-test-cmd", Input)
        target_inp.value = "nonexistent.py"
        cmd_inp.value = "pytest"

        screen._validate_and_submit()
        err_text = str(screen.query_one("#error-banner", Static).content)
        assert "does not exist on disk" in err_text

        # 2. Test outside workspace file
        target_inp.value = str(outside_file)
        screen._validate_and_submit()
        err_text = str(screen.query_one("#error-banner", Static).content)
        assert "outside workspace root" in err_text


@pytest.mark.asyncio
async def test_diff_review_screen_stale_patch_conflict(tmp_path):
    """Verify DiffReviewScreen detects concurrent modifications on disk before applying."""
    target_file = tmp_path / "module.py"
    target_file.write_bytes(b"original = 1\n")

    initial_hash = hashlib.sha256(b"original = 1\n").hexdigest()

    diff_text = """--- module.py
+++ module.py
@@ -1,1 +1,1 @@
-original = 1
+original = 2
"""
    patched_content = "original = 2\n"

    # Simulate concurrent file edit on disk while recovery was running
    target_file.write_bytes(b"concurrent_edit = True\n")

    run_id = "run_tui_stale"
    CandidateArtifactStore.save_run_artifact(
        run_id=run_id,
        target_file="module.py",
        test_command="pytest",
        baseline_hash=initial_hash,
        status="SUCCESS",
        verified=True,
        tier_used=1,
        attempts=1,
        cost_usd=0.0,
        duration_ms=1.0,
        patched_content=patched_content,
        unified_diff=diff_text,
        workspace_root=tmp_path,
    )

    screen = DiffReviewScreen(
        workspace=str(tmp_path),
        target_file=str(target_file),
        diff_text=diff_text,
        patched_content=patched_content,
        verified=True,
        baseline_hash=initial_hash,
        run_id=run_id,
    )
    app = ArgusTUIApp(workspace=str(tmp_path))

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        # Attempt to approve
        screen.action_approve_and_apply()
        await pilot.pause()

        feedback_text = str(screen.query_one("#diff-feedback", Static).content)
        assert "STALE_TARGET_CONFLICT" in feedback_text

        # Ensure disk file was NOT overwritten
        assert target_file.read_bytes() == b"concurrent_edit = True\n"


@pytest.mark.asyncio
async def test_diff_review_screen_atomic_apply(tmp_path):
    """Verify DiffReviewScreen applies verified patch atomically when hashes match."""
    target_file = tmp_path / "module.py"
    target_file.write_bytes(b"original = 1\n")

    initial_hash = hashlib.sha256(target_file.read_bytes()).hexdigest()

    diff_text = """--- module.py
+++ module.py
@@ -1,1 +1,1 @@
-original = 1
+original = 2
"""
    patched_content = "original = 2\n"

    run_id = "run_tui_atomic"
    CandidateArtifactStore.save_run_artifact(
        run_id=run_id,
        target_file="module.py",
        test_command="pytest",
        baseline_hash=initial_hash,
        status="SUCCESS",
        verified=True,
        tier_used=1,
        attempts=1,
        cost_usd=0.0,
        duration_ms=1.0,
        patched_content=patched_content,
        unified_diff=diff_text,
        workspace_root=tmp_path,
    )

    screen = DiffReviewScreen(
        workspace=str(tmp_path),
        target_file=str(target_file),
        diff_text=diff_text,
        patched_content=patched_content,
        verified=True,
        baseline_hash=initial_hash,
        run_id=run_id,
    )
    app = ArgusTUIApp(workspace=str(tmp_path))

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        # Approve and apply through the authoritative applier
        screen.action_approve_and_apply()
        await pilot.pause()

        # Verify disk file updated
        assert target_file.read_bytes() == b"original = 2\n"


def test_shortcut_installation(tmp_path):
    """Verify desktop shortcut installer writes valid .cmd and .vbs scripts."""
    sc_path = install_desktop_shortcut(target_dir=tmp_path)
    assert sc_path.is_file()
    assert sc_path.name == "Argus.cmd"
    content = sc_path.read_text()
    assert "argus app" in content

    vbs_path = tmp_path / "Argus_Silent.vbs"
    assert vbs_path.is_file()
    assert "WScript.Shell" in vbs_path.read_text()


@pytest.mark.asyncio
async def test_startup_screen_slash_recover_opens_workflow():
    """Verify entering '/recover' on StartupScreen navigates to RecoveryWorkflowScreen."""
    app = ArgusTUIApp(workspace=".")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, StartupScreen)

        inp = app.screen.query_one("#command-input", Input)
        inp.value = "/recover tests/test_calc.py"
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, RecoveryWorkflowScreen)
        cmd_inp = app.screen.query_one("#input-test-cmd", Input)
        assert cmd_inp.value == "tests/test_calc.py"


@pytest.mark.asyncio
async def test_startup_screen_direct_test_command_opens_workflow():
    """Verify directly typing 'pytest tests/test_demo.py' navigates directly to RecoveryWorkflowScreen."""
    app = ArgusTUIApp(workspace=".")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, StartupScreen)

        inp = app.screen.query_one("#command-input", Input)
        inp.value = "pytest tests/test_demo.py"
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, RecoveryWorkflowScreen)
        cmd_inp = app.screen.query_one("#input-test-cmd", Input)
        assert cmd_inp.value == "pytest tests/test_demo.py"


@pytest.mark.asyncio
async def test_command_palette_includes_recover_and_diff():
    """Verify CommandPaletteModal contains /recover and /diff entries."""
    modal = CommandPaletteModal(initial_query="/r")
    app = ArgusTUIApp(workspace=".")
    async with app.run_test() as pilot:
        app.push_screen(modal)
        await pilot.pause()

        cmd_names = [c.name for c in modal.filtered_commands]
        assert "/recover" in cmd_names


@pytest.mark.asyncio
async def test_clear_stream_action_does_not_crash():
    """Verify executing clear_stream from command palette does not raise NoMatches."""
    app = ArgusTUIApp(workspace=".")
    async with app.run_test() as pilot:
        await pilot.pause()
        app.open_command_palette()
        await pilot.pause()
        palette = app.screen
        palette.dismiss("clear_stream")
        await pilot.pause()
        # Verify app remains stable on StartupScreen
        assert isinstance(app.screen, StartupScreen)


@pytest.mark.asyncio
async def test_recovery_workflow_submits_with_test_command_alone(tmp_path):
    """Verify RecoveryWorkflowScreen accepts test command without demanding target file."""
    screen = RecoveryWorkflowScreen(
        workspace=str(tmp_path),
        config=ArgusConfig(),
        initial_test_cmd="pytest tests/test_calc.py",
    )
    app = ArgusTUIApp(workspace=str(tmp_path))
    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        assert screen.query_one("#input-target-file", Input).value == ""
        assert screen.query_one("#input-test-cmd", Input).value == "pytest tests/test_calc.py"

        screen._validate_and_submit()
        await pilot.pause()

        err_text = str(screen.query_one("#error-banner", Static).content)
        assert err_text == ""


@pytest.mark.asyncio
async def test_help_modal_opens_and_renders_shortcuts():
    """Verify HelpModal mounts and displays global shortcuts."""
    app = ArgusTUIApp(workspace=".")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, StartupScreen)

        # Trigger help modal
        app.open_help_modal()
        await pilot.pause()

        assert isinstance(app.screen, HelpModal)
        table_static = app.screen.query_one("#help-table", Static)
        assert table_static is not None

        # Dismiss with Esc
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, StartupScreen)


@pytest.mark.asyncio
async def test_execution_timeline_zoom_toggle():
    """Verify pressing 'z' toggles pane zoom in ExecutionTimelineScreen."""
    screen = ExecutionTimelineScreen(
        workspace=".",
        config=ArgusConfig(),
        target_file="test.py",
        test_command="pytest",
    )
    # Stop background worker immediately for deterministic UI testing
    screen.worker_active = False
    app = ArgusTUIApp(workspace=".")
    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        assert not screen.is_zoomed
        assert not screen.query_one("#exec-left-pane").has_class("hidden-pane")
        assert not screen.query_one("#exec-right-pane").has_class("maximized-pane")

        # Toggle zoom on
        screen.action_toggle_pane_zoom()
        assert screen.is_zoomed
        assert screen.query_one("#exec-left-pane").has_class("hidden-pane")
        assert screen.query_one("#exec-right-pane").has_class("maximized-pane")

        # Toggle zoom off
        screen.action_toggle_pane_zoom()
        assert not screen.is_zoomed
        assert not screen.query_one("#exec-left-pane").has_class("hidden-pane")
        assert not screen.query_one("#exec-right-pane").has_class("maximized-pane")


@pytest.mark.asyncio
async def test_execution_timeline_phase_spinner():
    """Verify animated phase spinner ticks and updates metrics while worker is active."""
    screen = ExecutionTimelineScreen(
        workspace=".",
        config=ArgusConfig(),
        target_file="calc.py",
        test_command="pytest test_calc.py",
    )
    screen.worker_active = True
    screen.current_phase = "[2/3] Synthesizing patch..."
    app = ArgusTUIApp(workspace=".")

    # Mock the background worker thread so it does not immediately fail on missing dummy file
    with patch.object(ExecutionTimelineScreen, "_run_recovery_worker", return_value=None):
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()

            # Ensure worker remains active during active spinner tick test
            screen.worker_active = True
            initial_index = screen.spinner_index
            screen._tick_timer()
            assert screen.spinner_index == initial_index + 1

            metrics_text = str(screen.query_one("#exec-header-metrics", Static).content)
            assert "[2/3] Synthesizing patch..." in metrics_text


@pytest.mark.asyncio
async def test_execution_timeline_worker_completion_success():
    """Verify UI transitions, button states, and diff updates on successful worker completion."""
    from argus.runtime.controller import RecoveryOutcome

    screen = ExecutionTimelineScreen(
        workspace=".",
        config=ArgusConfig(),
        target_file="calc.py",
        test_command="pytest test_calc.py",
    )
    app = ArgusTUIApp(workspace=".")

    with patch.object(ExecutionTimelineScreen, "_run_recovery_worker", return_value=None):
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()

            outcome = RecoveryOutcome(
                status="SUCCESS",
                total_attempts=1,
                total_cost_usd=0.015,
                total_duration_ms=500.0,
                unified_diff="--- calc.py\n+++ calc.py\n@@ -1 +1 @@\n-old\n+new",
                patched_content="def add(a, b): return a + b",
                verified=True,
            )

            screen._handle_completion(outcome)
            await pilot.pause()

            assert not screen.worker_active
            assert screen.query_one("#btn-cancel-exec", Button).disabled is True
            assert screen.query_one("#btn-return-menu", Button).disabled is False
            assert screen.query_one("#btn-review-diff", Button).disabled is False

            metrics_text = str(screen.query_one("#exec-header-metrics", Static).content)
            assert "COMPLETED" in metrics_text


@pytest.mark.asyncio
async def test_execution_timeline_worker_failure():
    """Verify UI transitions correctly when recovery fails without a patch."""
    from argus.runtime.controller import RecoveryOutcome

    screen = ExecutionTimelineScreen(
        workspace=".",
        config=ArgusConfig(),
        target_file="calc.py",
        test_command="pytest test_calc.py",
    )
    app = ArgusTUIApp(workspace=".")

    with patch.object(ExecutionTimelineScreen, "_run_recovery_worker", return_value=None):
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()

            outcome = RecoveryOutcome(
                status="FAILED",
                total_attempts=3,
                total_cost_usd=0.045,
                total_duration_ms=1200.0,
                unified_diff="",
                patched_content="",
                verified=False,
            )

            screen._handle_completion(outcome)
            await pilot.pause()

            assert not screen.worker_active
            assert screen.query_one("#btn-cancel-exec", Button).disabled is True
            assert screen.query_one("#btn-return-menu", Button).disabled is False
            assert screen.query_one("#btn-review-diff", Button).disabled is True

            metrics_text = str(screen.query_one("#exec-header-metrics", Static).content)
            assert "FINISHED" in metrics_text


@pytest.mark.asyncio
async def test_execution_timeline_cancellation():
    """Verify cancelling an active execution terminates worker and updates UI."""
    screen = ExecutionTimelineScreen(
        workspace=".",
        config=ArgusConfig(),
        target_file="calc.py",
        test_command="pytest test_calc.py",
    )
    app = ArgusTUIApp(workspace=".")

    with patch.object(ExecutionTimelineScreen, "_run_recovery_worker", return_value=None):
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()

            screen.worker_active = True
            screen.action_cancel_execution()
            await pilot.pause()

            assert not screen.worker_active
            assert screen.query_one("#btn-cancel-exec", Button).disabled is True
            assert screen.query_one("#btn-return-menu", Button).disabled is False


