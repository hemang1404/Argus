"""Regression tests for TUI hardening: routing, workspace propagation, cancellation, apply."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from unittest.mock import patch

import pytest
from rich.cells import cell_len
from textual.widgets import Button, Input, Static

from argus.config import ArgusConfig
from argus.runtime.candidate import ApplyResult, CandidateMetadata
from argus.runtime.controller import RecoveryOutcome
from argus.tui.app import ArgusTUIApp
from argus.tui.screens.diff_review import DiffReviewScreen
from argus.tui.screens.execution import ExecutionTimelineScreen
from argus.tui.screens.recovery import CONTEXT_LINE_MAX_WIDTH, RecoveryWorkflowScreen


@pytest.mark.asyncio
async def test_slash_diff_opens_diff_review_screen():
    """`/diff` must not raise TypeError (it previously passed an unsupported kwarg)."""
    app = ArgusTUIApp(workspace=".")
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.query_one("#command-input", Input).value = "/diff"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DiffReviewScreen)


@pytest.mark.asyncio
async def test_show_diff_screen_loads_requested_run(tmp_path):
    """A run id passed to `show_diff_screen` must select that run and carry its baseline."""
    app = ArgusTUIApp(workspace=str(tmp_path))
    metadata = CandidateMetadata(
        run_id="run_target",
        target_file="calc.py",
        test_command="pytest",
        baseline_hash="deadbeef",
        candidate_hash="cafebabe",
        status="SUCCESS",
        verified=True,
        tier_used=1,
        attempts=1,
        cost_usd=0.01,
        duration_ms=12.0,
        created_at="2026-11-04T00:00:00+00:00",
    )

    with patch(
        "argus.tui.app.CandidateArtifactStore.load_candidate",
        return_value=(metadata, "x = 2\n"),
    ) as load_candidate, patch(
        "argus.tui.app.CandidateArtifactStore.get_candidate_diff",
        return_value="--- a\n+++ b\n",
    ):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.show_diff_screen(run_id="run_target")
            await pilot.pause()

            screen = app.screen
            assert isinstance(screen, DiffReviewScreen)
            assert screen.run_id == "run_target"
            assert screen.baseline_hash == "deadbeef"
            assert screen.verified is True
            load_candidate.assert_called_once()


@pytest.mark.asyncio
async def test_slash_recover_keeps_runner_arguments_together():
    """`/recover pytest tests/helpers/api.py` must not strip the test path into a target."""
    app = ArgusTUIApp(workspace=".")
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.query_one("#command-input", Input).value = "/recover pytest tests/helpers/api.py"
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, RecoveryWorkflowScreen)
        assert app.screen.query_one("#input-test-cmd", Input).value == "pytest tests/helpers/api.py"
        assert app.screen.query_one("#input-target-file", Input).value == ""


@pytest.mark.asyncio
async def test_recovery_worker_receives_workspace_root_and_cancel_event(tmp_path):
    """Execution must run against the TUI workspace, not the process CWD."""
    screen = ExecutionTimelineScreen(
        workspace=str(tmp_path),
        config=ArgusConfig(),
        target_file="",
        test_command="pytest",
    )
    captured: dict = {}

    def fake_run(**kwargs) -> RecoveryOutcome:
        captured.update(kwargs)
        return RecoveryOutcome(
            status="SUCCESS",
            total_attempts=1,
            total_cost_usd=0.0,
            total_duration_ms=1.0,
        )

    app = ArgusTUIApp(workspace=str(tmp_path))
    with patch.object(ExecutionTimelineScreen, "_run_recovery_worker", return_value=None), patch(
        "argus.tui.screens.execution.RecoveryController"
    ) as controller_cls:
        controller_cls.return_value.run.side_effect = fake_run
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            outcome = screen._execute_recovery()

    assert outcome.status == "SUCCESS"
    assert captured["workspace_root"] == str(tmp_path)
    assert captured["cancel_event"] is screen._cancel_event
    assert captured["test_command"] == "pytest"


@pytest.mark.asyncio
async def test_cancelled_execution_ignores_late_outcome(tmp_path):
    """A result that lands after cancellation must not be reported or offered for review."""
    screen = ExecutionTimelineScreen(
        workspace=str(tmp_path),
        config=ArgusConfig(),
        target_file="calc.py",
        test_command="pytest",
    )
    app = ArgusTUIApp(workspace=str(tmp_path))

    with patch.object(ExecutionTimelineScreen, "_run_recovery_worker", return_value=None):
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()

            screen.action_cancel_execution()
            assert screen._cancel_event.is_set()

            late = RecoveryOutcome(
                status="SUCCESS",
                total_attempts=1,
                total_cost_usd=0.42,
                total_duration_ms=10.0,
                unified_diff="--- a\n+++ b\n",
                patched_content="x = 1\n",
                verified=True,
                run_id="run_late",
            )
            screen._handle_completion(late)
            await pilot.pause()

            metrics = str(screen.query_one("#exec-header-metrics", Static).content)
            assert "COMPLETED" not in metrics
            assert screen.query_one("#btn-review-diff", Button).disabled is True
            assert screen.outcome is None


@pytest.mark.asyncio
async def test_diff_apply_routes_through_authoritative_applier(tmp_path):
    """Approval must delegate to AuthoritativePatchApplier instead of writing directly."""
    target = tmp_path / "calc.py"
    target.write_text("original = 1\n")

    screen = DiffReviewScreen(
        workspace=str(tmp_path),
        target_file=str(target),
        run_id="run_abc",
        diff_text="--- a\n+++ b\n@@ -1 +1 @@\n-original = 1\n+original = 2\n",
        patched_content="original = 2\n",
        verified=True,
        baseline_hash="stale-baseline-value",
    )
    result = ApplyResult(
        success=True,
        run_id="run_abc",
        target_file=str(target),
        message="Applied verified patch.",
    )
    app = ArgusTUIApp(workspace=str(tmp_path))

    with patch("argus.tui.screens.diff_review.CandidateArtifactStore.set_approval") as approve, patch(
        "argus.tui.screens.diff_review.AuthoritativePatchApplier.apply",
        return_value=result,
    ) as apply_mock:
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            screen.action_approve_and_apply()
            await pilot.pause()

    approve.assert_called_once()
    kwargs = apply_mock.call_args.kwargs
    assert kwargs["run_id"] == "run_abc"
    assert kwargs["require_approval"] is True
    # The applier owns the write; the screen must not touch the file itself.
    assert target.read_text() == "original = 1\n"


@pytest.mark.asyncio
async def test_diff_apply_refuses_unverified_candidate(tmp_path):
    """An unverified candidate must never be written to disk from the TUI."""
    target = tmp_path / "calc.py"
    target.write_text("original = 1\n")

    screen = DiffReviewScreen(
        workspace=str(tmp_path),
        target_file=str(target),
        run_id="run_bad",
        diff_text="--- a\n+++ b\n",
        patched_content="original = 2\n",
        verified=False,
    )
    app = ArgusTUIApp(workspace=str(tmp_path))

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()
        screen.action_approve_and_apply()
        await pilot.pause()
        feedback = str(screen.query_one("#diff-feedback", Static).content).lower()

    assert "unverified" in feedback
    assert target.read_text() == "original = 1\n"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not meaningful on Windows")
async def test_direct_apply_preserves_file_mode(tmp_path):
    """The no-artifact fallback must not downgrade the target's permission bits."""
    target = tmp_path / "calc.py"
    target.write_text("original = 1\n")
    os.chmod(target, 0o640)
    baseline = hashlib.sha256(target.read_bytes()).hexdigest()

    screen = DiffReviewScreen(
        workspace=str(tmp_path),
        target_file=str(target),
        diff_text="--- a\n+++ b\n@@ -1 +1 @@\n-original = 1\n+original = 2\n",
        patched_content="original = 2\n",
        verified=True,
        baseline_hash=baseline,
    )
    app = ArgusTUIApp(workspace=str(tmp_path))

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()
        screen.action_approve_and_apply()
        await pilot.pause()

    assert target.read_text() == "original = 2\n"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o640


@pytest.mark.asyncio
async def test_policy_modal_persists_policy_json(tmp_path):
    """Saved routing policy must actually reach disk and be reloadable."""
    tiers = {
        "t1": {"name": "T1 FAST", "model": "gemini-3.8-flash-medium"},
        "t2": {"name": "T2 STRONG", "model": "claude-sonnet-4-6"},
        "t3": {"name": "T3 FRONTIER", "model": "claude-opus-4-6-thinking"},
    }
    app = ArgusTUIApp(workspace=".")

    with patch("argus.tui.modals.policy.resolve_dynamic_tiers", return_value=tiers), patch(
        "argus.providers.storage.get_config_dir", return_value=tmp_path
    ):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.open_policy_modal()
            await pilot.pause()
            app.screen.dismiss(
                {
                    "tier": "argus/cheap",
                    "budget": 0.25,
                    "attempts": 2,
                    "t1_model": "m1",
                    "t2_model": "m2",
                    "t3_model": "m3",
                }
            )
            await pilot.pause()

    policy_file = tmp_path / "policy.json"
    assert policy_file.is_file()
    data = json.loads(policy_file.read_text(encoding="utf-8"))
    assert data["tier"] == "argus/cheap"
    assert data["budget_limit_usd"] == 0.25
    assert data["max_attempts"] == 2
    assert app.current_tier == "argus/cheap"


@pytest.mark.asyncio
async def test_recovery_context_line_stays_on_one_row():
    """A long model id must not push 'Attempts 3' onto a second, centred line."""
    config = ArgusConfig(
        model="gemini-3.8-flash-medium",
        budget_limit_usd=0.03,
        max_attempts=3,
    )
    screen = RecoveryWorkflowScreen(workspace=".", config=config)
    app = ArgusTUIApp(workspace=".")

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()
        context = screen._format_context_text()

    assert "\n" not in context.plain
    assert cell_len(context.plain) <= CONTEXT_LINE_MAX_WIDTH
    assert "Attempts 3" in context.plain
    assert "Budget $0.03" in context.plain
