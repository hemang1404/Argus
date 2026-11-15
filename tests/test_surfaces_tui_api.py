"""Regression tests for TUI, API and MCP surfaces (no network, no provider, no host CLI)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from textual.widgets import Button, Static

from argus import __version__
from argus.api.recovery import MAX_API_BUDGET_USD, RecoveryTaskRequest
from argus.api.server import create_app
from argus.config import DEFAULT_BUDGET_USD, DEFAULT_MAX_ATTEMPTS, MAX_ATTEMPTS_CEILING, ArgusConfig
from argus.runtime.candidate import ApplyResult
from argus.runtime.controller import RecoveryOutcome
from argus.tui.app import ArgusTUIApp
from argus.tui.modals.doctor import DoctorModal
from argus.tui.modals.help import HelpModal
from argus.tui.modals.stats import StatsModal
from argus.tui.screens.diff_review import DiffReviewScreen
from argus.tui.widgets.status_bar import StatusBar


def _text(widget: Static) -> str:
    return str(widget.content)


def _screen_text(screen, width: int = 160) -> str:
    """All text currently held by the screen's Static widgets (rich renderables are exported as plain text)."""
    from rich.console import Console

    console = Console(width=width, record=True, force_terminal=False, file=io.StringIO())
    for static in screen.query(Static).results(Static):
        console.print(static.content)
    return " ".join(console.export_text().split())


# --------------------------------------------------------------------------- TUI
@pytest.mark.asyncio
async def test_question_mark_in_the_command_box_opens_help():
    app = ArgusTUIApp(workspace=".")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpModal)


@pytest.mark.asyncio
async def test_status_bar_shows_the_package_version_not_a_hardcoded_one():
    app = ArgusTUIApp(workspace=".")
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.screen.query_one(StatusBar)
        assert bar.version == f"v{__version__}"
        assert f"v{__version__}" in _text(app.screen.query_one("#descriptor", Static))


@pytest.mark.asyncio
async def test_doctor_does_not_report_ready_for_an_unconfigured_provider():
    app = ArgusTUIApp(workspace=".", config=ArgusConfig(provider=None, model=None))
    app.config.provider = None
    app.config.provider_config = None
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(DoctorModal(config=app.config))
        await pilot.pause()
        out = _screen_text(app.screen)
    assert "READY" not in out
    assert "SETUP NEEDED" in out
    assert "argus/auto" not in out


@pytest.mark.asyncio
async def test_doctor_configured_provider_is_not_claimed_connected():
    from argus.providers.models import ProviderConfig

    cfg = ProviderConfig(name="stubprov", base_url="http://127.0.0.1:9/v1", model="mock-model", api_key="k-123456789")
    config = ArgusConfig(provider_config=cfg, model="mock-model")
    app = ArgusTUIApp(workspace=".", config=config)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(DoctorModal(config=config))
        await pilot.pause()
        out = _screen_text(app.screen)
    assert "stubprov" in out
    assert "READY" not in out
    assert "not tested" in out


@pytest.mark.asyncio
async def test_idle_stats_modal_never_claims_a_running_gateway():
    app = ArgusTUIApp(workspace=".")
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(StatsModal(total_calls=0, prompt_tokens=0, completion_tokens=0, session_spend_usd=0.0, frontier_baseline_usd=0.0))
        await pilot.pause()
        screen_text = _screen_text(app.screen)
    assert "Gateway ready" not in screen_text
    assert "gateway listening" not in screen_text
    assert "Listening for inbound" not in screen_text
    assert "127.0.0.1:8000" not in screen_text


def _diff_screen(tmp_path: Path, run_id: str = "run_abc", verified: bool = True) -> tuple[DiffReviewScreen, Path]:
    target = tmp_path / "calc.py"
    target.write_text("original = 1\n")
    screen = DiffReviewScreen(
        workspace=str(tmp_path),
        target_file="calc.py",  # relative: must resolve against the workspace, not the process cwd
        run_id=run_id,
        diff_text="--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-original = 1\n+original = 2\n",
        patched_content="original = 2\n",
        verified=verified,
    )
    return screen, target


@pytest.mark.asyncio
async def test_diff_review_enter_never_approves_and_discard_is_focused(tmp_path: Path):
    screen, target = _diff_screen(tmp_path)
    app = ArgusTUIApp(workspace=str(tmp_path))
    with patch("argus.tui.screens.diff_review.CandidateArtifactStore.set_approval") as approve, patch(
        "argus.tui.screens.diff_review.AuthoritativePatchApplier.apply"
    ) as apply_mock:
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            assert screen.query_one("#btn-discard", Button).has_focus
            await pilot.press("enter")  # Enter on the focused Discard button leaves the screen
            await pilot.pause()
            assert not isinstance(app.screen, DiffReviewScreen)
    approve.assert_not_called()
    apply_mock.assert_not_called()
    assert target.read_text() == "original = 1\n"


@pytest.mark.asyncio
async def test_diff_review_needs_explicit_second_step_and_can_be_cancelled(tmp_path: Path):
    screen, target = _diff_screen(tmp_path)
    app = ArgusTUIApp(workspace=str(tmp_path))
    result = ApplyResult(success=True, run_id="run_abc", target_file=str(target), message="applied")
    with patch("argus.tui.screens.diff_review.CandidateArtifactStore.set_approval") as approve, patch(
        "argus.tui.screens.diff_review.AuthoritativePatchApplier.apply", return_value=result
    ) as apply_mock:
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()

            await pilot.press("a")  # step 1 only arms the confirmation
            await pilot.pause()
            approve.assert_not_called()
            apply_mock.assert_not_called()
            assert "y" in _text(screen.query_one("#diff-feedback", Static)).lower()

            await pilot.press("escape")  # cancels the pending approval, stays on the screen
            await pilot.pause()
            assert isinstance(app.screen, DiffReviewScreen)
            await pilot.press("y")  # nothing is armed any more
            await pilot.pause()
            approve.assert_not_called()

            await pilot.press("a")
            await pilot.press("y")  # step 2 confirms
            await pilot.pause()
    approve.assert_called_once()
    assert apply_mock.call_args.kwargs["expected_target"] == "calc.py"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["INVALID_RUN_ID", "CANDIDATE_SIGNATURE_INVALID"])
async def test_diff_review_reports_typed_candidate_errors(tmp_path: Path, code: str):
    from argus.runtime.candidate import CandidateSignatureError, InvalidRunIdError

    err = InvalidRunIdError() if code == "INVALID_RUN_ID" else CandidateSignatureError()
    screen, target = _diff_screen(tmp_path)
    app = ArgusTUIApp(workspace=str(tmp_path))
    with patch("argus.tui.screens.diff_review.CandidateArtifactStore.set_approval", side_effect=err):
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            screen.action_approve_and_apply()
            screen.action_confirm_apply()
            await pilot.pause()
            feedback = _text(screen.query_one("#diff-feedback", Static))
    assert code in feedback
    assert target.read_text() == "original = 1\n"


def test_tui_budget_default_is_the_config_default():
    import inspect

    from argus.tui.modals.stats import StatsModal as _S
    from argus.tui.modals.usage import UsageModal

    assert inspect.signature(_S.__init__).parameters["budget_limit_usd"].default == DEFAULT_BUDGET_USD
    assert inspect.signature(UsageModal.__init__).parameters["budget_limit_usd"].default == DEFAULT_BUDGET_USD


@pytest.mark.asyncio
async def test_execution_screen_shows_policy_notes_and_model_ladder(tmp_path: Path):
    from argus.tui.screens.execution import ExecutionTimelineScreen

    outcome = RecoveryOutcome(
        status="ALREADY_PASSING",
        total_attempts=0,
        total_cost_usd=0.0,
        total_duration_ms=1.0,
        config_notes=["tightened budget to 0.01"],
        routing_reasons=["model_ladder=explicit models=m1 escalation=off"],
    )
    app = ArgusTUIApp(workspace=str(tmp_path))
    with patch.object(ExecutionTimelineScreen, "_execute_recovery", return_value=outcome):
        async with app.run_test() as pilot:
            app.push_screen(
                ExecutionTimelineScreen(workspace=str(tmp_path), config=app.config, target_file="", test_command="pytest")
            )
            await pilot.pause()
            await pilot.pause()
            await pilot.pause(0.3)
            texts = " ".join(
                " ".join(str(s.content).split()) for s in app.screen.query(".event-entry").results(Static)
            )
    assert "tightened budget to 0.01" in texts
    assert "model_ladder=explicit models=m1 escalation=off" in texts


# --------------------------------------------------------------------------- API
def test_api_request_defaults_and_bounds_come_from_config():
    req = RecoveryTaskRequest(target_file="a.py", test_command="pytest")
    assert req.model is None  # no pseudo-model 'argus/auto'
    assert req.max_attempts == DEFAULT_MAX_ATTEMPTS
    assert req.budget == DEFAULT_BUDGET_USD
    for bad in (
        {"max_attempts": 0},
        {"max_attempts": MAX_ATTEMPTS_CEILING + 1},
        {"budget": 0},
        {"budget": -1},
        {"budget": float("nan")},
        {"budget": float("inf")},
        {"budget": MAX_API_BUDGET_USD + 1},
    ):
        with pytest.raises(ValueError):
            RecoveryTaskRequest(target_file="a.py", test_command="pytest", **bad)


def test_api_worker_runs_with_the_server_workspace_and_completes(tmp_path: Path):
    """The worker is referenced strongly, receives the API workspace and always sends RECOVERY_COMPLETE."""
    import argus.api.recovery as api_recovery

    root = tmp_path.resolve()
    target = root / "src" / "calc.py"
    target.parent.mkdir()
    target.write_text("def add(a, b): return a - b\n", encoding="utf-8")
    captured = {}

    def fake_run(self, target_file, test_command, test_file=None, workspace_root=None, **kwargs):
        captured["workspace_root"] = workspace_root
        captured["config"] = self.config
        return RecoveryOutcome(
            status="ALREADY_PASSING",
            total_attempts=0,
            total_cost_usd=0.0,
            total_duration_ms=1.0,
            config_notes=["note-from-policy"],
            target_file=str(target_file),
        )

    app = create_app(workspace=str(root), token="tok")
    headers = {"Authorization": "Bearer tok"}
    with contextlib.ExitStack() as stack:
        client = stack.enter_context(TestClient(app))
        stack.enter_context(patch("argus.api.recovery.RecoveryController.run", fake_run))
        resp = client.post(
            "/api/v1/recovery/run",
            headers=headers,
            json={"target_file": "src/calc.py", "test_command": "pytest"},
        )
        assert resp.status_code == 200
        task_id = resp.json()["task_id"]
        events = [
            json.loads(line[6:])
            for line in client.get(f"/api/v1/recovery/stream/{task_id}", headers=headers).iter_lines()
            if line.startswith("data: ")
        ]
    complete = [e for e in events if e["type"] == "RECOVERY_COMPLETE"]
    assert len(complete) == 1
    assert complete[0]["payload"]["status"] == "ALREADY_PASSING"
    assert complete[0]["payload"]["config_notes"] == ["note-from-policy"]
    assert Path(captured["workspace_root"]) == root
    assert captured["config"].model is None
    assert captured["config"].max_attempts == DEFAULT_MAX_ATTEMPTS
    assert not api_recovery._background_tasks  # finished workers are released


def test_api_worker_task_is_strongly_referenced():
    import argus.api.recovery as api_recovery

    async def scenario() -> bool:
        async def worker():
            await asyncio.sleep(0.01)

        task = asyncio.create_task(worker())
        api_recovery._background_tasks.add(task)
        task.add_done_callback(api_recovery._background_tasks.discard)
        held = task in api_recovery._background_tasks
        await task
        await asyncio.sleep(0)
        return held and task not in api_recovery._background_tasks

    assert asyncio.run(scenario())


def test_api_apply_surfaces_typed_candidate_errors(tmp_path: Path):
    from argus.runtime.candidate import CandidateSignatureError

    root = tmp_path.resolve()
    target = root / "calc.py"
    target.write_text("x = 1\n", encoding="utf-8")
    app = create_app(workspace=str(root), token="tok")
    import argus.api.recovery as api_recovery

    state = api_recovery.task_manager.create_task(
        RecoveryTaskRequest(target_file="calc.py", test_command="pytest"), root
    )
    state.outcome = RecoveryOutcome(
        status="SUCCESS",
        total_attempts=1,
        total_cost_usd=0.0,
        total_duration_ms=1.0,
        patched_content="x = 2\n",
        run_id="run_forged",
        target_file="calc.py",
    )
    with TestClient(app) as client, patch(
        "argus.api.recovery.CandidateArtifactStore.set_approval", side_effect=CandidateSignatureError()
    ):
        resp = client.post(
            "/api/v1/recovery/apply",
            headers={"Authorization": "Bearer tok"},
            json={"task_id": state.task_id, "target_file": "calc.py"},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"].startswith("CANDIDATE_SIGNATURE_INVALID")
    assert target.read_text(encoding="utf-8") == "x = 1\n"


# --------------------------------------------------------------------------- MCP consumption side
def test_mcp_recover_defaults_and_cap_come_from_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import inspect

    from argus.mcp import server as mcp_server

    monkeypatch.setenv("ARGUS_MCP_WORKSPACE_ROOT", str(tmp_path))
    server = mcp_server.create_mcp_server()
    tool = server._tool_manager.get_tool("argus_recover")
    sig = inspect.signature(tool.fn)
    assert sig.parameters["max_attempts"].default == DEFAULT_MAX_ATTEMPTS
    assert sig.parameters["budget"].default == DEFAULT_BUDGET_USD


def test_mcp_recover_reports_config_notes_and_caps_attempts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from argus.mcp import server as mcp_server
    from argus.providers.models import ProviderConfig

    seen = {}

    class _Controller:
        def __init__(self, config=None):
            seen["config"] = config

        def run(self, **kwargs):
            return RecoveryOutcome(
                status="ALREADY_PASSING",
                total_attempts=0,
                total_cost_usd=0.0,
                total_duration_ms=1.0,
                config_notes=["note-for-agent"],
            )

    monkeypatch.setenv("ARGUS_MCP_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(mcp_server, "RecoveryController", _Controller)
    server = mcp_server.create_mcp_server()
    fn = server._tool_manager.get_tool("argus_recover").fn
    from argus.providers.storage import ProviderStorage

    ProviderStorage().save_provider(
        ProviderConfig(name="stubprov", base_url="http://127.0.0.1:9/v1", model="mock-model", api_key="k-123456789")
    )
    payload = json.loads(fn(test_command="pytest", max_attempts=99))
    assert payload["config_notes"] == ["note-for-agent"]
    assert seen["config"].max_attempts == DEFAULT_MAX_ATTEMPTS  # an agent can lower, never raise


def test_mcp_get_candidate_reports_error_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from argus.mcp import server as mcp_server

    monkeypatch.setenv("ARGUS_MCP_WORKSPACE_ROOT", str(tmp_path))
    server = mcp_server.create_mcp_server()
    payload = json.loads(server._tool_manager.get_tool("argus_get_candidate").fn(run_id="../escape"))
    assert payload["error_code"] == "INVALID_RUN_ID"
    applied = json.loads(server._tool_manager.get_tool("argus_apply_candidate").fn(run_id="../escape"))
    assert applied["success"] is False


# --------------------------------------------------------------------------- web UI assets
def test_web_ui_has_no_hardcoded_version_or_external_fonts():
    html = (Path(__file__).resolve().parent.parent / "argus" / "ui" / "index.html").read_text(encoding="utf-8")
    assert "v0.2.0" not in html
    assert "fonts.googleapis.com" not in html
    assert "Connecting" in html
    assert 'id="version-badge"' in html
