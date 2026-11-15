"""Dedicated syntax-highlighted diff review and safe atomic application screen."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
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

    # Applying needs two deliberate steps: arm with `a` (or the Approve button), then
    # confirm with `y`. Enter never approves, so a stray keypress cannot modify a file.
    BINDINGS = [
        Binding("a", "approve_and_apply", "Approve"),
        Binding("y", "confirm_apply", "Confirm apply"),
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
        self._confirming = False

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

            if has_diff:
                with VerticalScroll(id="diff-scroll-pane"):
                    yield DiffViewer(diff_text=self.diff_text, id="diff-viewer-widget")
            else:
                # Kept outside the scroll pane: a VerticalScroll ignores the parent's
                # `align` rules, which left the glyph and command box flush left.
                with Vertical(id="diff-empty-state"):
                    # Each centred element gets its own full-width row: a container
                    # only applies `align` evenly when every child is full width.
                    with Horizontal(id="diff-empty-glyph-row"):
                        yield Static("±", id="diff-empty-glyph")
                    yield Static(
                        "No candidate patches are waiting for review.",
                        id="diff-empty-title",
                    )
                    yield Static(
                        "Run a test command from the home screen to synthesize a fix.",
                        id="diff-empty-copy",
                    )
                    with Horizontal(id="diff-empty-command-row"):
                        yield Static(
                            "[#8188ff]❯[/]  pytest tests/your_test.py   [#667085]↵[/]",
                            id="diff-empty-command",
                        )
                    yield Static(
                        "Argus reproduces the failure in a temporary copy of your project, then stages "
                        "the candidate patch here for review and approval.",
                        id="diff-empty-note",
                    )
                    yield Static(
                        "01 Reproduce   →   02 Synthesize   →   03 Verify",
                        id="diff-empty-flow",
                    )

            with Horizontal(id="diff-actions-bar"):
                yield Button("Discard (Esc)", id="btn-discard")
                if has_diff:
                    yield Button("Approve... (a)", variant="primary", id="btn-approve")

        yield StatusBar(workspace=self.workspace)

    def on_mount(self) -> None:
        # Discard is the safe default: a stray Enter on arrival must not approve anything.
        self.query_one("#btn-discard", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-approve":
            self.action_approve_and_apply()
        elif event.button.id == "btn-discard":
            self.action_discard_patch()

    def action_discard_patch(self) -> None:
        """Cancel a pending confirmation first; otherwise discard the patch and go back."""
        if self._confirming:
            self._disarm()
            self._report("[dim]Approval cancelled. Nothing was changed.[/dim]")
            return
        self.app.pop_screen()

    def _disarm(self) -> None:
        self._confirming = False
        try:
            self.query_one("#btn-approve", Button).label = "Approve... (a)"
        except NoMatches:
            pass

    def _report(self, markup: str) -> None:
        feedback = self.query_one("#diff-feedback", Static)
        feedback.add_class("-active")
        feedback.update(markup)

    def action_confirm_apply(self) -> None:
        """Second step: confirm the armed approval."""
        if self._confirming:
            self._apply_patch()

    def action_approve_and_apply(self) -> None:
        """First step: validate and ask for explicit confirmation; a second call applies."""
        if not self.diff_text.strip():
            self.app.pop_screen()
            return

        if self._confirming:
            self._apply_patch()
            return

        if self._validate_for_apply() is None:
            return

        self._confirming = True
        try:
            self.query_one("#btn-approve", Button).label = "Confirm: apply (y)"
        except NoMatches:
            pass
        self._report(
            f"[bold]Apply this patch to[/bold] {self._resolved_target()}? "
            "Press [bold]y[/bold] to approve and apply, or Esc to cancel."
        )

    def _resolved_target(self) -> Path:
        """Resolve the target against the workspace, not the process working directory."""
        path = Path(self.target_file)
        return path.resolve() if path.is_absolute() else (Path(self.workspace) / path).resolve()

    def _apply_patch(self) -> None:
        target_path = self._validate_for_apply()
        self._disarm()
        if target_path is not None:
            self._apply_via_artifact_store(target_path)

    def _validate_for_apply(self) -> Optional[Path]:
        """Return the resolved target when the candidate may be applied, else report why not."""
        if not self.target_file:
            self._report("[bold red]Error: No target file is associated with this candidate.[/bold red]")
            return None

        target_path = self._resolved_target()

        if not target_path.exists():
            self._report(f"[bold red]Error: Target file '{target_path}' does not exist.[/bold red]")
            return None

        if not self.patched_content:
            self._report("[bold red]Error: No patched content is available for this candidate.[/bold red]")
            return None

        if not self.verified:
            self._report(
                "[bold red]Refusing to apply an unverified patch:[/bold red] this candidate did not pass "
                "its test suite, so writing it could break the workspace. Re-run recovery, or use "
                "'argus approve <run-id> --force' from the CLI if you have reviewed it manually."
            )
            return None

        if not self.run_id:
            self._report(
                "[bold red]Refusing to apply:[/bold red] this candidate has no stored run "
                "artifact. Re-run recovery so the patch can be applied through the "
                "authoritative applier."
            )
            return None

        return target_path

    def _apply_via_artifact_store(self, target_path: Path) -> None:
        """Apply through the authoritative applier (hash, AST, containment, audit)."""
        root = Path(self.workspace).resolve()
        try:
            # The explicit two-step confirmation is the human review action. Record it as
            # interactive approval; the applier still performs every integrity check
            # (including the run-id and signature checks surfaced below).
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
