"""Dedicated syntax-highlighted diff review and safe atomic application screen."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Static

from argus.runtime.candidate import (
    AuthoritativePatchApplier,
    CandidateApplicationError,
    CandidateArtifactStore,
)
from argus.tui.widgets.diff_viewer import DiffViewer
from argus.tui.widgets.status_bar import StatusBar


class DiffReviewScreen(Screen[None]):
    """Screen for inspecting verified candidate patches in clean full-width layout."""

    BINDINGS = [
        Binding("a", "approve_and_apply", "Approve & Apply"),
        Binding("enter", "approve_and_apply", "Approve & Apply"),
        Binding("d", "discard_patch", "Discard"),
        Binding("escape", "discard_patch", "Back"),
    ]

    def __init__(
        self,
        workspace: str,
        target_file: str = "",
        diff_text: str = "",
        patched_content: str = "",
        verified: bool = True,
        baseline_hash: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.workspace = str(Path(workspace).resolve())
        self.target_file = target_file
        self.diff_text = diff_text
        self.patched_content = patched_content
        self.verified = verified
        self.baseline_hash = baseline_hash
        self.run_id = run_id

    def compose(self) -> ComposeResult:
        has_diff = bool(self.diff_text.strip())

        if has_diff:
            badge_text = (
                "[bold #7bc79e]● VERIFIED PASS[/]"
                if self.verified
                else "[bold #e07178]▲ UNVERIFIED[/]"
            )
            additions = sum(
                1 for line in self.diff_text.splitlines() if line.startswith("+") and not line.startswith("+++")
            )
            deletions = sum(
                1 for line in self.diff_text.splitlines() if line.startswith("-") and not line.startswith("---")
            )
            target_name = Path(self.target_file).name if self.target_file else "patch"
            title_text = (
                f"[#8188ff]DIFF[/]  [bold #e1e4ec]{target_name}[/]   "
                f"[bold #7bc79e]+{additions}[/] [bold #e07178]-{deletions}[/]   "
                f"[#667085]{self.target_file}[/]"
            )
        else:
            badge_text = "[#667085]○ no pending diff[/]"
            title_text = "[#8188ff]DIFF[/]  [bold #e1e4ec]No pending patches[/]"

        with Vertical(id="diff-outer"):
            with Horizontal(id="diff-header-bar"):
                yield Static(title_text, id="diff-title")
                yield Static(badge_text, id="diff-status-badge")

            yield Static("", id="diff-feedback")

            with VerticalScroll(id="diff-scroll-pane"):
                if has_diff:
                    yield DiffViewer(diff_text=self.diff_text, id="diff-viewer-widget")
                else:
                    with Vertical(id="diff-empty-state"):
                        yield Static("±", id="diff-empty-glyph")
                        yield Static(
                            "No candidate patches are waiting for review.",
                            id="diff-empty-title",
                        )
                        yield Static(
                            "Run a test command from the home screen to synthesize a fix.",
                            id="diff-empty-copy",
                        )
                        yield Static(
                            "[#8188ff]❯[/]  pytest tests/your_test.py   [#667085]↵[/]",
                            id="diff-empty-command",
                        )
                        yield Static(
                            "Argus reproduces the failure in an isolated git worktree, synthesizes "
                            "a patch, and stages it here for verification and approval.",
                            id="diff-empty-note",
                        )
                        yield Static(
                            "01 Reproduce   →   02 Synthesize   →   03 Verify",
                            id="diff-empty-flow",
                        )

            with Horizontal(id="diff-actions-bar"):
                yield Button("Discard (Esc)", id="btn-discard")
                if has_diff:
                    yield Button("Approve & Apply (a)", variant="primary", id="btn-approve")

        yield StatusBar(workspace=self.workspace, version="v0.2.0")

    def on_mount(self) -> None:
        if self.diff_text.strip() and self.verified:
            self.query_one("#btn-approve", Button).focus()
        else:
            self.query_one("#btn-discard", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-approve":
            self.action_approve_and_apply()
        elif event.button.id == "btn-discard":
            self.action_discard_patch()

    def action_discard_patch(self) -> None:
        """Discard the patch and return to the main screen."""
        self.app.pop_screen()

    def _report(self, markup: str) -> None:
        feedback = self.query_one("#diff-feedback", Static)
        feedback.add_class("-active")
        feedback.update(markup)

    def action_approve_and_apply(self) -> None:
        """Safely and atomically apply the verified patch to the target file."""
        if not self.diff_text.strip():
            self.app.pop_screen()
            return

        if not self.target_file:
            self._report("[bold red]Error: No target file is associated with this candidate.[/bold red]")
            return

        target_path = Path(self.target_file).resolve()

        if not target_path.exists():
            self._report(f"[bold red]Error: Target file '{target_path}' does not exist.[/bold red]")
            return

        if not self.patched_content:
            self._report("[bold red]Error: No patched content is available for this candidate.[/bold red]")
            return

        if not self.verified:
            self._report(
                "[bold red]Refusing to apply an unverified patch:[/bold red] this candidate did not pass "
                "its test suite, so writing it could break the workspace. Re-run recovery, or use "
                "'argus approve <run-id> --force' from the CLI if you have reviewed it manually."
            )
            return

        if not self.run_id:
            self._report(
                "[bold red]Refusing to apply:[/bold red] this candidate has no stored run "
                "artifact. Re-run recovery so the patch can be applied through the "
                "authoritative applier."
            )
            return

        self._apply_via_artifact_store(target_path)

    def _apply_via_artifact_store(self, target_path: Path) -> None:
        """Apply through the authoritative applier (hash, AST, containment, audit)."""
        root = Path(self.workspace).resolve()
        try:
            # The keypress is the human review action. Record it as interactive
            # approval; the applier still performs every integrity check.
            CandidateArtifactStore.set_approval(
                self.run_id or "",
                approved=True,
                workspace_root=root,
                approved_by="interactive:tui",
                approval_source="tui-diff-review",
            )
            result = AuthoritativePatchApplier.apply(
                run_id=self.run_id or "",
                workspace_root=root,
                expected_target=self.target_file or None,
                require_approval=True,
            )
        except CandidateApplicationError as err:
            self._report(f"[bold red]{err.error_code}:[/bold red] {err.message}")
            return
        except OSError as ex:
            self._report(f"[bold red]Application Failed:[/bold red] {ex}")
            return

        if result.success:
            self.app.notify(f"✓ {result.message}", severity="information")
            self.app.pop_screen()
            return

        self._report(
            f"[bold red]{result.error_code}:[/bold red] {result.message}\n"
            "[dim]Review the target file, then discard and re-run recovery if it changed on disk.[/dim]"
        )
