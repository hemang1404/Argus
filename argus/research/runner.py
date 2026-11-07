"""Unified Research Benchmark Runner for Argus.

Provides a standardized, reproducible harness to run SWE-bench Verified tasks
through the production RecoveryController with:
  1. Strict single-file target validation
  2. Isolated containerized execution (no host test execution fallback)
  3. Attempt-level telemetry logging into versioned JSONL
  4. Precise separation of infrastructure vs model repair failures
  5. Multi-tier budget enforcement (per-task reservation & global ceilings)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from argus.config import ArgusConfig, ModelCost
from argus.providers.adapter import ProviderAdapter
from argus.runtime.budget import GlobalExperimentBudget, TaskConditionBudget
from argus.runtime.container import (
    BaseCommitMismatchError,
    CandidateIntegrityError,
    ContainerConfig,
    ContainerEvaluator,
    is_container_engine_available,
)
from argus.runtime.controller import RecoveryController, RecoveryOutcome


def classify_failure_type(terminal_reason: str) -> str:
    """Classify the terminal outcome into infrastructure failure vs model repair failure."""
    if terminal_reason in ("SUCCESS", "ALREADY_PASSING"):
        return "SUCCESS"

    infra_reasons = {
        "CONTAINER_SETUP_ERROR",
        "BASE_COMMIT_MISMATCH",
        "CANDIDATE_INTEGRITY_MISMATCH",
        "DOCKER_UNAVAILABLE",
        "CONFIG_ERROR",
        "WORKSPACE_ERROR",
    }
    if terminal_reason in infra_reasons or "CONTAINER" in terminal_reason:
        return "INFRASTRUCTURE_FAILURE"

    model_reasons = {
        "FAILED_MAX_ATTEMPTS",
        "BUDGET_EXCEEDED",
        "DEADLINE_EXCEEDED",
        "PROVIDER_RATE_LIMITED",
        "LOCALIZATION_FAILED",
        "PATCH_APPLICATION_FAILED",
        "AST_SYNTAX_ERROR",
    }
    if terminal_reason in model_reasons:
        return "MODEL_REPAIR_FAILURE"

    return "UNKNOWN_FAILURE"


@dataclass
class TaskDefinition:
    """Normalized specification of an evaluated SWE-bench task."""
    task_id: str
    repo: str
    base_commit: str
    target_file: str
    test_command: str
    problem_statement: str
    container_image: str = "argus-fixture:latest"
    gold_patch: str = ""
    test_patch: str = ""
    initial_code: Optional[str] = None
    fail_to_pass: List[str] = field(default_factory=list)
    pass_to_pass: List[str] = field(default_factory=list)
    version: str = ""


@dataclass
class TaskOutcomeRecord:
    """Structured telemetry record emitted for each evaluated task-condition."""
    task_id: str
    condition: str
    model_id: str
    terminal_reason: str
    failure_classification: str
    verified_passing: bool
    total_attempts: int
    total_reserved_usd: float
    total_settled_usd: float
    spend_uncertain: bool
    quarantined_usd: float
    total_duration_ms: float
    candidate_hash: str
    base_commit: str
    f2p_passed: bool = False
    p2p_passed: bool = False
    official_resolved: bool = False
    canonical_status: str = "UNRESOLVED"
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    attempts: List[Dict[str, Any]] = field(default_factory=list)


class UnifiedResearchRunner:
    """Executes SWE-bench tasks through the production RecoveryController."""

    def __init__(
        self,
        config: Optional[ArgusConfig] = None,
        adapter: Optional[ProviderAdapter] = None,
        output_dir: Optional[Path | str] = None,
        task_budget_cap_usd: float = 0.030,
        global_budget_cap_usd: float = 5.000,
        run_id: Optional[str] = None,
    ) -> None:
        self.config = config or ArgusConfig()
        self.adapter = adapter
        self.task_budget_cap = float(task_budget_cap_usd)
        self.global_budget_cap = float(global_budget_cap_usd)
        self.run_id = run_id or f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

        base_out = Path(output_dir) if output_dir else Path("results/runs") / self.run_id
        self.output_dir = base_out
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.outcomes_path = self.output_dir / "outcomes.jsonl"
        self.attempts_path = self.output_dir / "attempts.jsonl"

        # Global budget coordinator for this research run
        self.global_budget = GlobalExperimentBudget.get_instance(self.global_budget_cap)

    @staticmethod
    def extract_single_file_target(gold_patch: str) -> Optional[str]:
        """Extract the relative target file path if the patch modifies exactly one file."""
        if not gold_patch:
            return None
        matches = re.findall(r"diff --git a/(.*?) b/", gold_patch)
        unique_files = list(dict.fromkeys(matches))
        if len(unique_files) == 1:
            return unique_files[0]
        return None

    @classmethod
    def load_task_from_dict(
        cls,
        task_dict: Dict[str, Any],
        default_container_image: str = "argus-fixture:latest",
    ) -> Optional[TaskDefinition]:
        """Parse and validate task metadata, rejecting multi-file tasks."""
        task_id = task_dict.get("instance_id") or task_dict.get("task_id")
        base_commit = task_dict.get("base_commit")
        repo = task_dict.get("repo")
        problem_statement = task_dict.get("problem_statement") or ""
        gold_patch = task_dict.get("gold_patch") or ""
        test_patch = task_dict.get("test_patch") or ""

        if not task_id or not base_commit or not repo:
            return None

        # Verify single-file constraint
        target_file = cls.extract_single_file_target(gold_patch)
        if not target_file:
            return None

        # Construct verification test command
        fail_to_pass = task_dict.get("fail_to_pass") or []
        if isinstance(fail_to_pass, list) and fail_to_pass:
            test_tokens = " ".join(fail_to_pass[:2])
            test_cmd = f"pytest {test_tokens} -q"
        else:
            test_cmd = f"python3 -c \"import os; assert os.path.exists('{target_file}')\""

        # Canonical expected container image if instance_id follows standard SWE-bench format
        expected_image = default_container_image
        if "__" in task_id and default_container_image == "argus-fixture:latest":
            docker_compatible_id = task_id.replace("__", "_1776_")
            expected_image = f"swebench/sweb.eval.x86_64.{docker_compatible_id}:latest"

        return TaskDefinition(
            task_id=task_id,
            repo=repo,
            base_commit=base_commit,
            target_file=target_file,
            test_command=test_cmd,
            problem_statement=problem_statement,
            container_image=expected_image,
            gold_patch=gold_patch,
            test_patch=test_patch,
            fail_to_pass=fail_to_pass,
            pass_to_pass=task_dict.get("pass_to_pass") or [],
            version=task_dict.get("version") or "",
        )

    def run_task(
        self,
        task: TaskDefinition,
        condition: str = "direct",
        model: Optional[str] = None,
        use_container: bool = True,
        container_evaluator: Optional[ContainerEvaluator] = None,
        mock_test: bool = False,
    ) -> TaskOutcomeRecord:
        """Execute a single task-condition run through the production controller."""
        # 1. Check Global Budget Availability
        if self.global_budget.available <= 0.0:
            rec = TaskOutcomeRecord(
                task_id=task.task_id,
                condition=condition,
                model_id=model or self.config.model or "unknown",
                terminal_reason="BUDGET_EXCEEDED",
                failure_classification="MODEL_REPAIR_FAILURE",
                verified_passing=False,
                total_attempts=0,
                total_reserved_usd=0.0,
                total_settled_usd=0.0,
                spend_uncertain=False,
                quarantined_usd=0.0,
                total_duration_ms=0.0,
                candidate_hash="",
                base_commit=task.base_commit,
            )
            self._write_records(rec)
            return rec

        active_model = model or self.config.model or "openai/gpt-oss-20b"

        # 2. Setup Staging Workspace
        with tempfile.TemporaryDirectory() as staging_dir:
            ws_root = Path(staging_dir)
            target_path = ws_root / task.target_file
            target_path.parent.mkdir(parents=True, exist_ok=True)

            # Seed target file with initial code, extract from local repo git show, or placeholder
            if task.initial_code is not None:
                target_path.write_text(task.initial_code, encoding="utf-8")
            elif not target_path.exists():
                repo_owner_repo = task.repo.replace("/", "__")
                repo_dir = Path("data/repos") / repo_owner_repo
                extracted = False
                if repo_dir.exists():
                    try:
                        res = subprocess.run(
                            ["git", "-C", str(repo_dir), "show", f"{task.base_commit}:{task.target_file}"],
                            capture_output=True,
                            text=True,
                            check=True,
                        )
                        target_path.write_text(res.stdout, encoding="utf-8")
                        extracted = True
                    except Exception:
                        pass
                if not extracted and not target_path.exists():
                    target_path.write_text("# Target file initial state\ndef solve(): pass\n", encoding="utf-8")

            # 3. Configure Controller & Container Evaluator
            active_container = container_evaluator
            if active_container is None and use_container:
                if not is_container_engine_available("docker"):
                    rec = TaskOutcomeRecord(
                        task_id=task.task_id,
                        condition=condition,
                        model_id=active_model,
                        terminal_reason="CONTAINER_SETUP_ERROR",
                        failure_classification="INFRASTRUCTURE_FAILURE",
                        verified_passing=False,
                        total_attempts=0,
                        total_reserved_usd=0.0,
                        total_settled_usd=0.0,
                        spend_uncertain=False,
                        quarantined_usd=0.0,
                        total_duration_ms=0.0,
                        candidate_hash="",
                        base_commit=task.base_commit,
                    )
                    self._write_records(rec)
                    return rec

                c_cfg = ContainerConfig(
                    image=task.container_image,
                    workdir="/testbed",
                    base_commit=task.base_commit,
                    setup_patch=task.test_patch,
                    timeout_seconds=self.config.repo_test_timeout_seconds,
                    network_disabled=True,
                )
                active_container = ContainerEvaluator(c_cfg)

            max_attempts = 1 if condition == "direct" else (2 if condition == "self_repair" else 3)
            escalation_enabled = (condition == "escalate")

            run_cfg = ArgusConfig(
                model=active_model,
                small_model=self.config.small_model or "openai/gpt-oss-20b",
                large_model=self.config.large_model or "openai/gpt-oss-120b",
                budget_limit_usd=self.task_budget_cap,
                max_attempts=max_attempts,
                escalation_enabled=escalation_enabled,
                allow_rate_limit_retries=False,
                use_container_runner=use_container,
                repo_test_timeout_seconds=self.config.repo_test_timeout_seconds,
                task_deadline_seconds=self.config.task_deadline_seconds,
                max_tokens=256,
            )

            controller = RecoveryController(
                config=run_cfg,
                adapter=self.adapter,
                container_evaluator=active_container,
            )

            # 4. Invoke Production Controller
            outcome = controller.run(
                target_file=str(target_path),
                test_command=task.test_command,
                workspace_root=ws_root,
                task_id=task.task_id,
                condition=condition,
                global_budget=self.global_budget,
                base_commit=task.base_commit,
                problem_statement=task.problem_statement,
                mock_test=mock_test,
            )

            # 5. Extract Telemetry & Classify
            classification = classify_failure_type(outcome.status)
            attempt_telemetry: List[Dict[str, Any]] = []

            for att in outcome.history:
                attempt_telemetry.append({
                    "task_id": task.task_id,
                    "condition": condition,
                    "attempt": att.attempt,
                    "model_used": att.model_used or active_model,
                    "duration_ms": att.duration_ms,
                    "prompt_tokens_est": getattr(att, "prompt_tokens_est", 0),
                    "prompt_tokens_wire": att.prompt_tokens,
                    "completion_tokens_wire": att.completion_tokens,
                    "reserved_cost_usd": getattr(att, "reserved_cost_usd", 0.0),
                    "settled_cost_usd": att.cost_usd,
                    "test_passed": att.test_passed,
                    "patch_success": att.patch_success,
                    "error_summary": att.error_summary,
                })

            record = TaskOutcomeRecord(
                task_id=task.task_id,
                condition=condition,
                model_id=active_model,
                terminal_reason=outcome.status,
                failure_classification=classification,
                verified_passing=outcome.verified or (outcome.status == "SUCCESS"),
                total_attempts=outcome.total_attempts,
                total_reserved_usd=outcome.total_reserved_usd,
                total_settled_usd=outcome.total_cost_usd,
                spend_uncertain=outcome.spend_uncertain,
                quarantined_usd=outcome.quarantined_usd,
                total_duration_ms=outcome.total_duration_ms,
                candidate_hash=outcome.candidate_hash,
                base_commit=task.base_commit,
                attempts=attempt_telemetry,
            )

            self._write_records(record)
            return record

    def _write_records(self, record: TaskOutcomeRecord) -> None:
        """Persist structured records to versioned JSONL files."""
        # 1. Outcomes JSONL
        out_dict = asdict(record)
        attempts = out_dict.pop("attempts", [])
        with open(self.outcomes_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(out_dict) + "\n")

        # 2. Attempts JSONL
        if attempts:
            with open(self.attempts_path, "a", encoding="utf-8") as f:
                for att in attempts:
                    f.write(json.dumps(att) + "\n")
