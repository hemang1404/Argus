"""Focused real-time execution timeline screen with OpenCode split-pane layout."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Static

from argus.config import ArgusConfig
from argus.runtime.controller import AttemptRecord, RecoveryController, RecoveryOutcome
from argus.tui.widgets.status_bar import StatusBar

# Terminal (non-success) statuses that carry an explanatory outcome.message.
_EXPLAINED_FAILURES = (
    "TEST_SETUP_ERROR",
    "LOCALIZATION_FAILED",
    "CONFIG_ERROR",
    "INVALID_TARGET",
    "CONTAINER_SETUP_ERROR",
    "PROVIDER_RATE_LIMITED",
    "DEADLINE_EXCEEDED",
    "CANCELLED",
)


class ExecutionTimelineScreen(Screen[None]):
    """Screen displaying real-time recovery execution events and metrics in split-pane layout."""

    BINDINGS = [
        Binding("escape", "handle_escape", "Back / Cancel"),
        Binding("ctrl+c", "cancel_execution", "Cancel"),
        Binding("z", "toggle_pane_zoom", "Zoom Diff"),
        Binding("d", "open_diff_review", "Diff", show=False),
    ]

    def __init__(
        self,
        workspace: str,
        config: ArgusConfig,
        target_file: str,
        test_command: str,
        test_file: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.workspace = workspace
        self.config = config
        self.target_file = target_file
        self.test_command = test_command
        self.test_file = test_file

        self.outcome: Optional[RecoveryOutcome] = None
        self.worker_active: bool = True
        self.start_time: float = 0.0
        self.cumulative_cost: float = 0.0
        self.current_attempt: int = 0
        self.is_zoomed: bool = False
        self.spinner_index: int = 0
        self.current_phase: str = "[1/3] Localizing failure points..."
        self.SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

        # Cooperative cancellation: checked by RecoveryController between attempts.
        self._cancel_event = threading.Event()
        self._cancelled = False

    def compose(self) -> ComposeResult:
        target_name = Path(self.target_file).name if self.target_file else "Auto-Localize"
        with Vertical(id="exec-outer"):
            with Horizontal(id="exec-header-bar"):
                yield Static(f"argus run · [bold]{target_name}[/bold]", id="exec-header-title")
                yield Static("Initializing recovery loop...", id="exec-header-metrics")

            with Horizontal(id="exec-body"):
                with VerticalScroll(id="exec-left-pane"):
                    yield Static("Timeline", classes="pane-title")
                    yield Static(self._format_init_event(), classes="event-entry", id="event-init")

                with VerticalScroll(id="exec-right-pane"):
                    yield Static("Patch Synthesis Stream", classes="pane-title")
                    yield Static("[dim]Waiting for candidate patch synthesis...[/dim]", id="exec-stream-content")

            with Horizontal(id="exec-actions-bar"):
                yield Button("Cancel", variant="error", id="btn-cancel-exec")
                yield Button("Zoom Diff (z)", variant="default", id="btn-zoom-pane")
                yield Button("Review Diff", variant="primary", id="btn-review-diff", disabled=True)
                yield Button("Return", variant="default", id="btn-return-menu", disabled=True)

        yield StatusBar(workspace=self.workspace, version="v0.2.0")

    def on_mount(self) -> None:
        """Start execution worker on mount."""
        self.start_time = time.perf_counter()
        self.set_interval(0.5, self._tick_timer)
        self._run_recovery_worker()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel-exec":
            self.action_cancel_execution()
        elif event.button.id == "btn-zoom-pane":
            self.action_toggle_pane_zoom()
        elif event.button.id == "btn-review-diff":
            self._open_diff_review()
        elif event.button.id == "btn-return-menu":
            self.app.pop_screen()

    def action_handle_escape(self) -> None:
        """Handle Esc key."""
        if self.worker_active:
            self.action_cancel_execution()
        else:
            self.app.pop_screen()

    def action_toggle_pane_zoom(self) -> None:
        """Toggle maximizing the patch stream pane to 100% full width."""
        self.is_zoomed = not self.is_zoomed
        left_pane = self.query_one("#exec-left-pane", VerticalScroll)
        right_pane = self.query_one("#exec-right-pane", VerticalScroll)
        zoom_btn = self.query_one("#btn-zoom-pane", Button)

        if self.is_zoomed:
            left_pane.add_class("hidden-pane")
            right_pane.add_class("maximized-pane")
            zoom_btn.label = "Restore (z)"
        else:
            left_pane.remove_class("hidden-pane")
            right_pane.remove_class("maximized-pane")
            zoom_btn.label = "Zoom Diff (z)"

    def action_open_diff_review(self) -> None:
        if self._cancelled:
            return
        if self.outcome and self.outcome.patched_content:
            self._open_diff_review()

    def action_cancel_execution(self) -> None:
        """Request cancellation of the active recovery execution.

        The controller can only stop between attempts (a provider call already in
        flight cannot be interrupted), so the worker is told to stop and its result
        is ignored if it arrives after the user cancelled.
        """
        if not self.worker_active:
            self.app.pop_screen()
            return

        self._cancelled = True
        self._cancel_event.set()
        self.worker_active = False
        self.current_phase = "Cancelling after the in-flight step..."
        self._append_event(
            "[bold red][CANCELLED][/bold red] Cancellation requested — "
            "stopping after the current step. No patch will be applied."
        )
        self._finalize_ui(success=False, has_patch=False)

        if self.is_mounted:
            self.query_one("#exec-header-metrics", Static).update(
                "[bold #f85149]⊘ CANCELLED[/bold #f85149] | "
                f"Spend so far: [bold #3fb950]${self.cumulative_cost:.5f}[/bold #3fb950]"
            )

    def _tick_timer(self) -> None:
        """Update elapsed timer and animated pulse spinner in header."""
        if not self.worker_active or not self.is_mounted:
            return
        elapsed = time.perf_counter() - self.start_time
        frame = self.SPINNER_FRAMES[self.spinner_index % len(self.SPINNER_FRAMES)]
        self.spinner_index += 1

        metrics = self.query_one("#exec-header-metrics", Static)
        metrics.update(
            f"[bold #818cf8]{frame}[/bold #818cf8] [bold #818cf8]{self.current_phase}[/bold #818cf8] | "
            f"Attempt: [bold yellow]{self.current_attempt}/{self.config.max_attempts}[/bold yellow] | "
            f"Spend: [bold #3fb950]${self.cumulative_cost:.5f}[/bold #3fb950] | "
            f"Elapsed: [white]{elapsed:.1f}s[/white]"
        )

    def _format_init_event(self) -> Text:
        t = Text()
        t.append("● Target: ", style="bold #818cf8")
        t.append(f"{self.target_file or 'auto-localize'}\n", style="dim")
        t.append("● Command: ", style="bold #818cf8")
        t.append(f"{self.test_command}\n", style="yellow")
        t.append("● Workspace: ", style="bold #818cf8")
        t.append(f"{self.workspace}\n", style="dim")
        t.append("● Status: ", style="bold #818cf8")
        t.append("Reproducing baseline failure...", style="dim")
        return t

    def _append_event(self, text: str) -> None:
        """Append a new timeline entry into the left pane (UI thread only)."""
        if not self.is_mounted:
            return
        left_pane = self.query_one("#exec-left-pane", VerticalScroll)
        left_pane.mount(Static(text, classes="event-entry"))
        left_pane.scroll_end(animate=False)

    def _update_stream_content(self, text: str) -> None:
        """Update the right pane live diff/code stream (UI thread only)."""
        if not self.is_mounted:
            return
        self.query_one("#exec-stream-content", Static).update(text)

    def _execute_recovery(self) -> RecoveryOutcome:
        """Run the controller synchronously, with callbacks marshalled to the UI thread."""
        controller = RecoveryController(config=self.config)

        def on_start(attempt: int, max_attempts: int) -> None:
            self.app.call_from_thread(self._on_attempt_start_ui, attempt, max_attempts)

        def on_end(record: AttemptRecord) -> None:
            self.app.call_from_thread(self._on_attempt_end_ui, record)

        return controller.run(
            target_file=self.target_file if self.target_file else None,
            test_command=self.test_command,
            test_file=self.test_file,
            workspace_root=self.workspace,
            on_attempt_start=on_start,
            on_attempt_end=on_end,
            cancel_event=self._cancel_event,
        )

    @work(exclusive=True, thread=True)
    def _run_recovery_worker(self) -> None:
        """Run RecoveryController in a dedicated background thread."""
        try:
            outcome = self._execute_recovery()
        except Exception as ex:
            self.app.call_from_thread(
                self._append_event,
                f"[bold red]Unexpected Execution Error:[/bold red] {ex}",
            )
            self.app.call_from_thread(self._finalize_ui, False, False)
            return

        self.app.call_from_thread(self._handle_completion, outcome)

    def _on_attempt_start_ui(self, attempt: int, max_attempts: int) -> None:
        """Record attempt start on the UI thread."""
        self.current_attempt = attempt
        self.current_phase = f"[2/3] Synthesizing patch via {self.config.model}..."
        msg = (
            f"[bold #818cf8]── Attempt {attempt}/{max_attempts} ──[/bold #818cf8]\n"
            f"  Generating repair patch via [bold]{self.config.model}[/bold]..."
        )
        self._append_event(msg)

    def _on_attempt_end_ui(self, record: AttemptRecord) -> None:
        """Record attempt result on the UI thread."""
        self.cumulative_cost += record.cost_usd
        self.current_phase = "[3/3] Executing verification test suite..."
        status_text = "[bold green]PASS[/bold green]" if record.test_passed else "[bold red]FAIL[/bold red]"
        tier_info = f"Tier {record.tier_used}" if record.tier_used else "No Patch"
        msg = (
            f"  [dim]Result:[/dim] {status_text} | "
            f"[dim]Reconciliation:[/dim] {tier_info} | "
            f"[dim]Tokens:[/dim] {record.prompt_tokens + record.completion_tokens} | "
            f"[dim]Cost:[/dim] ${record.cost_usd:.5f} | "
            f"[dim]Duration:[/dim] {record.duration_ms / 1000:.2f}s"
        )
        if not record.test_passed and record.error_summary:
            first_err_line = record.error_summary.strip().splitlines()[0]
            msg += f"\n  [red]Feedback:[/red] [dim]{first_err_line[:90]}[/dim]"
        self._append_event(msg)

    def _handle_completion(self, outcome: RecoveryOutcome) -> None:
        """Handle outcome on the main UI thread."""
        self.worker_active = False
        elapsed = time.perf_counter() - self.start_time

        if self._cancelled:
            # The user already cancelled: never present this as a result, and never
            # offer a patch for review after they asked to stop.
            self._append_event(
                "[bold red][CANCELLED][/bold red] Recovery stopped. "
                f"Spend: ${outcome.total_cost_usd:.5f}. No patch will be applied."
            )
            self._finalize_ui(success=False, has_patch=False)
            # Count the spend that already happened; a cancelled run still costs money.
            self.app.record_session_run(outcome)
            return

        self.outcome = outcome

        if not self.is_mounted:
            return

        self.current_phase = "Recovery loop completed"
        has_patch = bool(outcome.unified_diff and outcome.patched_content)

        metrics = self.query_one("#exec-header-metrics", Static)
        if outcome.status == "SUCCESS":
            status_badge = "[bold #3fb950]✓ COMPLETED[/bold #3fb950]"
        elif outcome.status == "ALREADY_PASSING":
            status_badge = "[bold #d97706]✓ ALREADY PASSING[/bold #d97706]"
        elif outcome.status == "CANCELLED":
            status_badge = "[bold #f85149]⊘ CANCELLED[/bold #f85149]"
        else:
            status_badge = "[bold #f85149]✗ FINISHED[/bold #f85149]"
        metrics.update(
            f"{status_badge} | Spend: [bold #3fb950]${outcome.total_cost_usd:.5f}[/bold #3fb950] | "
            f"Elapsed: [white]{elapsed:.1f}s[/white]"
        )

        if outcome.status == "SUCCESS":
            msg = (
                f"\n[bold green]RECOVERY SUCCEEDED![/bold green] Verified passing in attempt {outcome.total_attempts}.\n"
                f"Total spend: [bold green]${outcome.total_cost_usd:.5f}[/bold green] in {elapsed:.1f}s."
            )
        elif outcome.status == "ALREADY_PASSING":
            msg = "\n[bold yellow]Target test is ALREADY PASSING without any code modifications.[/bold yellow]"
        elif outcome.status == "BUDGET_EXCEEDED":
            msg = f"\n[bold red]BUDGET EXCEEDED[/bold red]: Reached configured cap of ${self.config.budget_limit_usd:.4f}."
        elif outcome.status in _EXPLAINED_FAILURES:
            msg = f"\n[bold red]{outcome.status}:[/bold red] {outcome.message or 'Execution aborted.'}"
        else:
            msg = f"\n[bold red]RECOVERY FAILED[/bold red]: Max attempts ({outcome.total_attempts}) exhausted."

        self._append_event(msg)
        if outcome.unified_diff:
            self._update_stream_content(outcome.unified_diff)

        self._finalize_ui(success=(outcome.status == "SUCCESS"), has_patch=has_patch)
        self.app.record_session_run(outcome)

    def _finalize_ui(self, success: bool, has_patch: bool) -> None:
        """Enable post-execution buttons."""
        if not self.is_mounted:
            return
        self.query_one("#btn-cancel-exec", Button).disabled = True
        self.query_one("#btn-return-menu", Button).disabled = False

        if has_patch:
            review_btn = self.query_one("#btn-review-diff", Button)
            review_btn.disabled = False
            review_btn.focus()
        else:
            self.query_one("#btn-return-menu", Button).focus()

    def _open_diff_review(self) -> None:
        """Navigate to DiffReviewScreen."""
        if not self.outcome or not self.outcome.patched_content:
            return
        self.app.open_diff_review(
            target_file=self.outcome.target_file or self.target_file,
            diff_text=self.outcome.unified_diff,
            patched_content=self.outcome.patched_content,
            verified=(self.outcome.status == "SUCCESS"),
            run_id=self.outcome.run_id or None,
            baseline_hash=self.outcome.baseline_hash or None,
        )
