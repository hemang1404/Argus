"""Argus Terminal Application root module.

Coordinates screens, global keybindings, and in-process execution state.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding

from argus.config import ArgusConfig
from argus.runtime.candidate import CandidateApplicationError, CandidateArtifactStore
from argus.runtime.controller import RecoveryOutcome
from argus.tui.modals.config import ConfigModal
from argus.tui.modals.connect import ConnectModal
from argus.tui.modals.doctor import DoctorModal
from argus.tui.modals.help import HelpModal
from argus.tui.modals.palette import CommandPaletteModal
from argus.tui.modals.policy import PolicyModal
from argus.tui.modals.stats import StatsModal
from argus.tui.modals.update import UpdateModal
from argus.tui.modals.usage import UsageModal
from argus.tui.screens.diff_review import DiffReviewScreen
from argus.tui.screens.execution import ExecutionTimelineScreen
from argus.tui.screens.recovery import RecoveryWorkflowScreen
from argus.tui.screens.startup import StartupScreen


class ArgusTUIApp(App[None]):
    """Main Textual application for Argus Terminal UI."""

    CSS_PATH = "styles.tcss"
    TITLE = "Argus"
    SUB_TITLE = "Adaptive Code Recovery Runtime"

    BINDINGS = [
        Binding("ctrl+c", "quit_app", "Exit", show=False),
        Binding("ctrl+p", "open_palette", "Commands", show=True),
        Binding("ctrl+k", "open_palette", "Palette", show=True),
        Binding("question_mark", "open_help_modal", "Help", show=True),
    ]

    def __init__(
        self,
        workspace: str = ".",
        config: Optional[ArgusConfig] = None,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.workspace = str(Path(workspace).resolve())
        self.config = config or ArgusConfig()
        self.current_tier: str = "argus/auto"

        # Load persisted policy if present
        try:
            from argus.providers.storage import get_config_dir
            policy_file = get_config_dir() / "policy.json"
            if policy_file.is_file():
                p_data = json.loads(policy_file.read_text(encoding="utf-8"))
                if "tier" in p_data:
                    self.current_tier = str(p_data["tier"])
                if "budget_limit_usd" in p_data:
                    self.config.budget_limit_usd = float(p_data["budget_limit_usd"])
                if "max_attempts" in p_data:
                    self.config.max_attempts = int(p_data["max_attempts"])
                if "model" in p_data:
                    self.config.model = str(p_data["model"])
        except (OSError, TypeError, ValueError):
            # Unreadable or corrupt policy file: keep defaults rather than crashing startup.
            pass

        # Session tracking state
        self.session_attempts: int = 0
        self.session_prompt_tokens: int = 0
        self.session_completion_tokens: int = 0
        self.session_cost_usd: float = 0.0

        # Last execution state
        self.last_target_file: Optional[str] = None
        self.last_test_command: Optional[str] = None
        self.last_outcome: Optional[RecoveryOutcome] = None

    def on_mount(self) -> None:
        """Mount the initial StartupScreen."""
        self.push_screen(StartupScreen(workspace=self.workspace, config=self.config))

    def action_quit_app(self) -> None:
        """Exit the application cleanly."""
        self.exit()

    def action_open_palette(self, initial_query: str = "") -> None:
        """Trigger the command palette modal."""
        self.open_command_palette(initial_query=initial_query)

    def action_open_policy(self) -> None:
        """Trigger the routing policy modal."""
        self.open_policy_modal()

    def open_command_palette(self, initial_query: str = "") -> None:
        """Open the searchable slash-command palette."""
        def handle_action(action_key: Optional[str]) -> None:
            if not action_key:
                return

            if action_key == "config_policy":
                self.open_policy_modal()
            elif action_key == "show_connect":
                self.open_connect_modal()
            elif action_key in ("show_stats", "show_usage"):
                self.open_stats_modal()
            elif action_key == "show_doctor":
                self.open_doctor_modal()
            elif action_key in ("show_update", "update"):
                self.open_update_modal()
            elif action_key == "clear_stream":
                if isinstance(self.screen, StartupScreen):
                    self.screen.stream_records = []
                self.notify("Stream history cleared.", severity="information")
            elif action_key == "recover":
                self.start_recovery_workflow()
            elif action_key == "config_model":
                self._open_config_modal("model")
            elif action_key == "config_tier":
                self._open_config_modal("tier")
            elif action_key == "config_budget":
                self._open_config_modal("budget")
            elif action_key == "config_attempts":
                self._open_config_modal("attempts")
            elif action_key == "show_diff":
                self.show_diff_screen()
            elif action_key in ("show_help", "help"):
                self.open_help_modal()
            elif action_key == "quit_app":
                self.exit()

        self.push_screen(CommandPaletteModal(initial_query=initial_query), handle_action)

    def action_open_help_modal(self) -> None:
        """Trigger the help & cheatsheet modal."""
        self.open_help_modal()

    def open_help_modal(self) -> None:
        """Open HelpModal displaying shortcuts and navigation cheat-sheet."""
        self.push_screen(HelpModal())

    def open_policy_modal(self) -> None:
        """Open unified PolicyModal."""
        def handle_policy(result: Optional[dict[str, Any]]) -> None:
            if not result:
                return
            tier = result.get("tier", "argus/auto")
            budget = result.get("budget", 0.50)
            attempts = result.get("attempts", 3)

            self.current_tier = tier
            self.config.budget_limit_usd = float(budget)
            self.config.max_attempts = int(attempts)

            # Resolve tier model dynamically based on discovered active tiers
            t1 = result.get("t1_model")
            t2 = result.get("t2_model")
            t3 = result.get("t3_model")
            if not t1 or not t2 or not t3:
                from argus.providers.discovery import resolve_dynamic_tiers
                dyn = resolve_dynamic_tiers()
                t1 = t1 or dyn["t1"]["model"]
                t2 = t2 or dyn["t2"]["model"]
                t3 = t3 or dyn["t3"]["model"]

            tier_models = {
                "argus/auto": t1,
                "argus/cheap": t1,
                "t1": t1,
                "argus/strong": t2,
                "t2": t2,
                "argus/frontier": t3,
                "t3": t3,
            }
            if tier in tier_models:
                self.config.model = tier_models[tier]

            try:
                from argus.providers.storage import get_config_dir
                policy_file = get_config_dir() / "policy.json"
                policy_data = {
                    "tier": tier,
                    "budget_limit_usd": budget,
                    "max_attempts": attempts,
                    "model": self.config.model,
                }
                policy_file.write_text(json.dumps(policy_data, indent=2), encoding="utf-8")
            except OSError as ex:
                self.notify(
                    f"Policy applied for this session, but could not be saved: {ex}",
                    severity="warning",
                )
            else:
                self.notify(
                    f"Policy saved: {tier} (Cap: ${budget:.2f}, Attempts: {attempts})",
                    severity="information",
                )
            if isinstance(self.screen, StartupScreen):
                self.screen.update_status()

        self.push_screen(PolicyModal(current_config=self.config, current_tier=self.current_tier), handle_policy)

    def open_connect_modal(self) -> None:
        """Open ConnectModal with MCP and proxy configs."""
        self.push_screen(ConnectModal())

    def open_stats_modal(self) -> None:
        """Open StatsModal displaying live token savings and economics."""
        from argus.gateway.telemetry import TelemetryRegistry
        summary = TelemetryRegistry.get().get_summary()
        self.push_screen(
            StatsModal(
                total_calls=summary.total_calls,
                prompt_tokens=summary.total_prompt_tokens,
                completion_tokens=summary.total_completion_tokens,
                session_spend_usd=summary.session_spend_usd,
                frontier_baseline_usd=summary.frontier_cost_usd,
                budget_limit_usd=self.config.budget_limit_usd,
            )
        )

    def open_doctor_modal(self) -> None:
        """Open DoctorModal."""
        self.push_screen(DoctorModal(config=self.config))

    def action_open_update(self) -> None:
        """Trigger the update manager modal."""
        self.open_update_modal()

    def open_update_modal(self) -> None:
        """Open UpdateModal to check and apply version updates."""
        self.push_screen(UpdateModal())

    def _open_config_modal(self, config_type: str) -> None:
        """Open modal for updating a specific configuration value."""
        def handle_config_result(result: Optional[dict[str, Any]]) -> None:
            if not result:
                return
            ctype = result.get("type")
            cval = result.get("value")

            if ctype == "model":
                self.config.model = str(cval)
                p_val = result.get("provider")
                if p_val:
                    self.config.provider = str(p_val)
                self.notify(f"Active model set to: {cval}", severity="information")
            elif ctype == "tier":
                self.current_tier = str(cval)
                from argus.providers.discovery import resolve_dynamic_tiers
                dyn = resolve_dynamic_tiers()
                t1 = dyn["t1"]["model"]
                t2 = dyn["t2"]["model"]
                t3 = dyn["t3"]["model"]
                tier_models = {
                    "argus/auto": t1,
                    "argus/cheap": t1,
                    "t1": t1,
                    "argus/strong": t2,
                    "t2": t2,
                    "argus/frontier": t3,
                    "t3": t3,
                }
                if str(cval) in tier_models:
                    self.config.model = tier_models[str(cval)]
                self.notify(f"Routing tier configured: {cval}", severity="information")
            elif ctype == "budget":
                self.config.budget_limit_usd = float(cval)
                self.notify(f"Budget cap set to: ${float(cval):.4f}", severity="information")
            elif ctype == "attempts":
                self.config.max_attempts = int(cval)
                self.notify(f"Max recovery attempts set to: {cval}", severity="information")

            # Update startup screen if active
            if isinstance(self.screen, StartupScreen):
                self.screen.update_status()

        self.push_screen(ConfigModal(config_type=config_type, current_config=self.config), handle_config_result)

    def start_recovery_workflow(
        self,
        target_file: str = "",
        test_cmd: str = "",
        test_command: str = "",
    ) -> None:
        """Navigate to the guided recovery parameter input screen."""
        effective_cmd = test_command or test_cmd
        self.push_screen(
            RecoveryWorkflowScreen(
                workspace=self.workspace,
                config=self.config,
                initial_target=target_file,
                initial_test_cmd=effective_cmd,
            )
        )

    def launch_recovery(
        self,
        target_file: Optional[str] = None,
        test_cmd: str = "",
        test_file: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Alias for execute_recovery supporting optional target_file."""
        self.execute_recovery(
            target_file=target_file,
            test_command=test_cmd or kwargs.get("test_command", ""),
            test_file=test_file,
        )

    def execute_recovery(
        self,
        target_file: Optional[str] = None,
        test_command: str = "",
        test_file: Optional[str] = None,
    ) -> None:
        """Launch the live execution timeline screen with background worker."""
        self.last_target_file = target_file
        self.last_test_command = test_command
        self.push_screen(
            ExecutionTimelineScreen(
                workspace=self.workspace,
                config=self.config,
                target_file=target_file or "",
                test_command=test_command,
                test_file=test_file,
            )
        )

    def open_diff_review(
        self,
        target_file: str,
        diff_text: str,
        patched_content: str,
        verified: bool = True,
        run_id: Optional[str] = None,
        baseline_hash: Optional[str] = None,
    ) -> None:
        """Navigate to the diff review screen."""
        self.push_screen(
            DiffReviewScreen(
                workspace=self.workspace,
                target_file=target_file,
                diff_text=diff_text,
                patched_content=patched_content,
                verified=verified,
                baseline_hash=baseline_hash,
                run_id=run_id,
            )
        )

    def show_diff_screen(self, run_id: Optional[str] = None) -> None:
        """Open diff review for a specific run, the active outcome, or the newest candidate."""
        ws_path = Path(self.workspace)

        if run_id:
            try:
                metadata, patched_content = CandidateArtifactStore.load_candidate(run_id, ws_path)
                diff_text = CandidateArtifactStore.get_candidate_diff(metadata.run_id, ws_path)
            except (CandidateApplicationError, OSError, ValueError) as ex:
                self.notify(f"Could not load candidate '{run_id}': {ex}", severity="warning")
                return
            self.open_diff_review(
                target_file=metadata.target_file,
                diff_text=diff_text or "",
                patched_content=patched_content or "",
                verified=metadata.verified,
                run_id=metadata.run_id,
                baseline_hash=metadata.baseline_hash,
            )
            return

        if self.last_outcome and self.last_outcome.patched_content:
            self.open_diff_review(
                target_file=self.last_target_file or self.last_outcome.target_file or "",
                diff_text=self.last_outcome.unified_diff or "",
                patched_content=self.last_outcome.patched_content,
                verified=(self.last_outcome.status == "SUCCESS"),
                run_id=self.last_outcome.run_id or None,
                baseline_hash=self.last_outcome.baseline_hash or None,
            )
            return

        try:
            candidates = CandidateArtifactStore.list_candidates(ws_path)
        except OSError:
            candidates = []

        if candidates:
            cand = candidates[0]
            try:
                diff_text = CandidateArtifactStore.get_candidate_diff(cand.run_id, ws_path)
                _, patched_content = CandidateArtifactStore.load_candidate(cand.run_id, ws_path)
            except (CandidateApplicationError, OSError, ValueError):
                diff_text, patched_content = "", None
            self.open_diff_review(
                target_file=cand.target_file,
                diff_text=diff_text or "",
                patched_content=patched_content or "",
                verified=cand.verified,
                run_id=cand.run_id,
                baseline_hash=cand.baseline_hash,
            )
            return

        # If no candidates exist, open DiffReviewScreen with empty state & guidance
        self.open_diff_review(
            target_file="",
            diff_text="",
            patched_content="",
            verified=False,
        )

    def record_session_run(self, outcome: RecoveryOutcome) -> None:
        """Record telemetry from a completed recovery run."""
        self.last_outcome = outcome
        self.session_attempts += outcome.total_attempts
        self.session_cost_usd += outcome.total_cost_usd
        for rec in outcome.history:
            self.session_prompt_tokens += rec.prompt_tokens
            self.session_completion_tokens += rec.completion_tokens


def main() -> None:
    """CLI entrypoint for running the Argus TUI."""
    app = ArgusTUIApp()
    app.run()


if __name__ == "__main__":
    main()
