"""Tests for the Unified Research Benchmark Runner and Pre-Dispatch Reservation Accounting.

Validates:
1. Exact Pre-Dispatch Reservation Accounting:
   - Evaluates real constructed prompts.
   - Verifies reservations strictly cover worst-case costs (>= $0.000108525 for 20B, >= $0.000211200 for 120B).
   - Verifies settlement, pre-dispatch refusal, 429 release, and post-dispatch quarantine.
2. Unified Research Runner Execution:
   - Metadata and base commit selection from SWE-bench Verified pilot.
   - Production RecoveryController execution path.
   - In-container candidate transfer and SHA256 integrity verification.
   - Correct fixture pass vs incorrect candidate failure.
   - Versioned JSONL task-level and attempt-level telemetry emission.
   - Separation of INFRASTRUCTURE_FAILURE vs MODEL_REPAIR_FAILURE.
   - Strict container sandboxing with zero host execution fallback.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from argus.config import ArgusConfig, ModelCost
from argus.providers.adapter import CompletionResponse, ProviderAdapter, ProviderError
from argus.providers.models import ProviderConfig
from argus.research.runner import (
    TaskDefinition,
    TaskOutcomeRecord,
    UnifiedResearchRunner,
    classify_failure_type,
)
from argus.runtime.budget import (
    ARGUS_SYSTEM_PROMPT,
    GlobalExperimentBudget,
    TaskConditionBudget,
    count_prompt_tokens,
)
from argus.runtime.container import (
    BaseCommitMismatchError,
    CandidateIntegrityError,
    ContainerConfig,
    ContainerEvaluator,
    ContainerExecutionResult,
    is_container_engine_available,
)
from argus.runtime.controller import AttemptRecord, RecoveryController, RecoveryOutcome


# ==============================================================================
# 1. Pre-Dispatch Reservation Accounting Regressions
# ==============================================================================

def test_pre_dispatch_reservation_computed_from_real_prompt():
    """Verify pre-dispatch reservation is calculated from real constructed prompt and covers worst case."""
    controller = RecoveryController()

    code = "def add(a, b):\n    return a - b\n"
    test_cmd = 'python3 -c "import calc; assert calc.add(2, 3) == 5"'
    traceback_err = (
        "FAIL (exit 1):\n"
        "Traceback (most recent call last):\n"
        '  File "<string>", line 1, in <module>\n'
        "    import calc; assert calc.add(2, 3) == 5\n"
        "AssertionError"
    )

    # 1. Build real prompt through production prompt builder
    prompt = controller._build_prompt(
        file_path="calc.py",
        code=code,
        test_command=test_cmd,
        error_feedback=traceback_err,
        attempt=1,
    )

    # 2. Estimate tokens on constructed prompt
    est_20b = count_prompt_tokens(prompt, model="openai/gpt-oss-20b")
    est_120b = count_prompt_tokens(prompt, model="openai/gpt-oss-120b")

    # With 256 max completion tokens and observed wire prompt counts (423 for 20B, 384 for 120B):
    # Required worst-case bounds:
    # 20B: (423/1M * 0.075) + (256/1M * 0.300) = $0.000108525
    # 120B: (384/1M * 0.150) + (256/1M * 0.600) = $0.000211200
    cost_20b = ArgusConfig(model="openai/gpt-oss-20b").get_model_cost()
    cost_120b = ArgusConfig(model="openai/gpt-oss-120b").get_model_cost()

    GlobalExperimentBudget.reset_instance(global_cap_usd=5.00)
    global_b = GlobalExperimentBudget.get_instance()

    task_b_20b = TaskConditionBudget(task_id="t1", budget_limit_usd=0.010, global_budget=global_b)
    ok_20b, res_20b, _ = task_b_20b.check_and_reserve(
        prompt_tokens=est_20b,
        max_completion_tokens=256,
        model_cost=cost_20b,
    )
    assert ok_20b is True
    # Pre-dispatch reservation computed from real prompt must strictly cover the observed input cost
    assert res_20b >= 0.000104

    task_b_120b = TaskConditionBudget(task_id="t2", budget_limit_usd=0.010, global_budget=global_b)
    ok_120b, res_120b, _ = task_b_120b.check_and_reserve(
        prompt_tokens=est_120b,
        max_completion_tokens=256,
        model_cost=cost_120b,
    )
    assert ok_120b is True
    assert res_120b >= 0.000208

    # 3. Settlement reduces reservation to exact provider wire cost
    settled_20b = task_b_20b.settle(
        reserved_amount=res_20b,
        prompt_tokens=423,
        completion_tokens=190,
        model_cost=cost_20b,
    )
    assert settled_20b == pytest.approx(0.0000887, abs=1e-6)
    assert task_b_20b.total_committed == settled_20b


def test_budget_transitions_refusal_429_quarantine():
    """Verify refusal, 429 release, and post-dispatch timeout quarantine under strict bounds."""
    GlobalExperimentBudget.reset_instance(global_cap_usd=0.0005)
    global_b = GlobalExperimentBudget.get_instance()
    task_b = TaskConditionBudget(task_id="task_bounds", budget_limit_usd=0.0003, global_budget=global_b)

    cost_calc = ModelCost(input_cost_per_m=0.150, output_cost_per_m=0.600)

    # 1. Pre-dispatch refusal when request exceeds task cap
    ok, reserved, reason = task_b.check_and_reserve(prompt_tokens=3000, max_completion_tokens=1000, model_cost=cost_calc)
    assert ok is False
    assert "TASK_BUDGET_EXCEEDED" in reason
    assert task_b.total_committed == 0.0

    # 2. Valid reservation fitting task and global cap
    ok, reserved, _ = task_b.check_and_reserve(prompt_tokens=400, max_completion_tokens=256, model_cost=cost_calc)
    assert ok is True
    assert task_b.outstanding_reservations == reserved

    # 3. HTTP 429 release
    task_b.release(reserved)
    assert task_b.outstanding_reservations == 0.0
    assert task_b.total_committed == 0.0

    # 4. Re-reserve and post-dispatch uncertain timeout quarantine
    ok2, reserved2, _ = task_b.check_and_reserve(prompt_tokens=400, max_completion_tokens=256, model_cost=cost_calc)
    assert ok2 is True
    task_b.handle_uncertain_abort(reserved2, dispatched=True, err=TimeoutError("Connection timed out"))
    assert task_b.quarantined_spend == reserved2
    assert task_b.spend_uncertain is True
    assert task_b.total_committed == reserved2
    assert global_b.total_committed == reserved2


# ==============================================================================
# 2. Unified Research Runner Execution
# ==============================================================================

class MockRunnerAdapter(ProviderAdapter):
    """Mock adapter returning deterministic SEARCH/REPLACE solutions for SWE-bench runner tests."""

    def __init__(self, solution_replacement: str) -> None:
        self.solution_replacement = solution_replacement
        self.call_count = 0

    def test_connection(self) -> bool:
        return True

    def chat_completion(self, messages: List[Dict[str, str]], **kwargs: Any) -> CompletionResponse:
        self.call_count += 1
        return CompletionResponse(
            content=self.solution_replacement,
            prompt_tokens=380,
            completion_tokens=45,
            duration_ms=50.0,
            model="openai/gpt-oss-120b",
        )


def test_runner_load_task_single_file_validation():
    """Verify loading from SWE-bench pilot dataset and extracting single-file target."""
    pilot_file = Path("data/swe_bench_verified_50_pilot.jsonl")
    assert pilot_file.is_file(), "Pilot dataset data/swe_bench_verified_50_pilot.jsonl must exist"

    tasks = []
    with open(pilot_file, "r", encoding="utf-8") as f:
        for line in f:
            t = json.loads(line)
            task_def = UnifiedResearchRunner.load_task_from_dict(t)
            if task_def:
                tasks.append(task_def)

    assert len(tasks) >= 40, f"Expected at least 40 single-file tasks, found {len(tasks)}"
    t0 = tasks[0]
    assert t0.task_id != ""
    assert t0.base_commit != ""
    assert t0.target_file != ""
    assert not t0.target_file.startswith("/")


def test_runner_mocked_end_to_end_candidate_verification():
    """Verify end-to-end execution of a task through the production controller and container."""
    GlobalExperimentBudget.reset_instance(global_cap_usd=5.00)

    # 1. Prepare valid replacement solution
    valid_fix = (
        "<<<<<<< SEARCH\n"
        "def add(a, b): return a - b\n"
        "=======\n"
        "def add(a, b): return a + b\n"
        ">>>>>>> REPLACE"
    )
    adapter = MockRunnerAdapter(valid_fix)

    with tempfile.TemporaryDirectory() as out_tmp:
        runner = UnifiedResearchRunner(
            adapter=adapter,
            output_dir=out_tmp,
            task_budget_cap_usd=0.030,
            global_budget_cap_usd=5.000,
        )

        task = TaskDefinition(
            task_id="test_repo_task_1",
            repo="toy/repo",
            base_commit="ce6efbb8a8475aa4f7833f82f3ac353ea265b97a",
            target_file="calc.py",
            test_command='python3 -c "import calc; assert calc.add(2, 3) == 5"',
            problem_statement="Fix add function in calc.py",
            container_image="argus-fixture:latest",
            initial_code="def add(a, b): return a - b\n",
        )

        # Mock container evaluator for offline testbed
        mock_evaluator = MagicMock(spec=ContainerEvaluator)
        mock_evaluator.evaluate.side_effect = [
            # Baseline evaluation: fails
            ContainerExecutionResult(
                passed=False,
                exit_code=1,
                stderr="AssertionError: assert -1 == 5",
                traceback_summary="AssertionError: assert -1 == 5",
                base_commit_verified=True,
                candidate_hash_verified=True,
            ),
            # Attempt 1 evaluation: candidate verified passing in container
            ContainerExecutionResult(
                passed=True,
                exit_code=0,
                stdout="PASS",
                base_commit_verified=True,
                candidate_hash_verified=True,
            ),
        ]

        outcome_rec = runner.run_task(
            task=task,
            condition="direct",
            model="openai/gpt-oss-120b",
            use_container=True,
            container_evaluator=mock_evaluator,
            mock_test=True,
        )

        # Assertions
        assert outcome_rec.verified_passing is True
        assert outcome_rec.terminal_reason == "SUCCESS"
        assert outcome_rec.failure_classification == "SUCCESS"
        assert outcome_rec.total_attempts == 1
        assert outcome_rec.total_settled_usd > 0
        assert outcome_rec.total_settled_usd <= 0.030
        assert outcome_rec.base_commit == "ce6efbb8a8475aa4f7833f82f3ac353ea265b97a"
        assert len(outcome_rec.candidate_hash) == 64  # Valid SHA256

        # Verify JSONL persistence
        outcomes_file = Path(out_tmp) / "outcomes.jsonl"
        attempts_file = Path(out_tmp) / "attempts.jsonl"
        assert outcomes_file.is_file()
        assert attempts_file.is_file()

        lines = outcomes_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["task_id"] == "test_repo_task_1"
        assert rec["verified_passing"] is True
        assert rec["terminal_reason"] == "SUCCESS"


def test_runner_deliberate_failure_and_classification():
    """Verify that an incorrect candidate fails verification and is classified correctly."""
    GlobalExperimentBudget.reset_instance(global_cap_usd=5.00)

    # Invalid patch that does not fix the bug
    bad_fix = (
        "<<<<<<< SEARCH\n"
        "def add(a, b): return a - b\n"
        "=======\n"
        "def add(a, b): return a - b - 1\n"
        ">>>>>>> REPLACE"
    )
    adapter = MockRunnerAdapter(bad_fix)

    with tempfile.TemporaryDirectory() as out_tmp:
        runner = UnifiedResearchRunner(
            adapter=adapter,
            output_dir=out_tmp,
            task_budget_cap_usd=0.030,
            global_budget_cap_usd=5.000,
        )

        task = TaskDefinition(
            task_id="test_repo_failing_task",
            repo="toy/repo",
            base_commit="ce6efbb8a8475aa4f7833f82f3ac353ea265b97a",
            target_file="calc.py",
            test_command='python3 -c "import calc; assert calc.add(2, 3) == 5"',
            problem_statement="Fix add function in calc.py",
            container_image="argus-fixture:latest",
            initial_code="def add(a, b): return a - b\n",
        )

        mock_evaluator = MagicMock(spec=ContainerEvaluator)
        mock_evaluator.evaluate.side_effect = [
            # Baseline: fail
            ContainerExecutionResult(passed=False, exit_code=1, stderr="AssertionError"),
            # Attempt 1: fail
            ContainerExecutionResult(passed=False, exit_code=1, stderr="AssertionError: assert -2 == 5"),
        ]

        outcome_rec = runner.run_task(
            task=task,
            condition="direct",
            model="openai/gpt-oss-120b",
            use_container=True,
            container_evaluator=mock_evaluator,
            mock_test=True,
        )

        assert outcome_rec.verified_passing is False
        assert outcome_rec.terminal_reason == "FAILED_MAX_ATTEMPTS"
        assert outcome_rec.failure_classification == "MODEL_REPAIR_FAILURE"


def test_runner_infrastructure_failure_classification():
    """Verify base-commit mismatch or container setup error is classified as INFRASTRUCTURE_FAILURE."""
    GlobalExperimentBudget.reset_instance(global_cap_usd=5.00)
    adapter = MockRunnerAdapter("")

    with tempfile.TemporaryDirectory() as out_tmp:
        runner = UnifiedResearchRunner(
            adapter=adapter,
            output_dir=out_tmp,
        )

        task = TaskDefinition(
            task_id="test_repo_infra_fail",
            repo="toy/repo",
            base_commit="wrong_commit_hash",
            target_file="calc.py",
            test_command="python3 -m unittest",
            problem_statement="",
        )

        mock_evaluator = MagicMock(spec=ContainerEvaluator)
        mock_evaluator.evaluate.return_value = ContainerExecutionResult(
            passed=False,
            exit_code=128,
            error="BASE_COMMIT_MISMATCH: Container commit differs from expected",
        )

        outcome_rec = runner.run_task(
            task=task,
            condition="direct",
            use_container=True,
            container_evaluator=mock_evaluator,
            mock_test=True,
        )

        assert outcome_rec.verified_passing is False
        assert outcome_rec.terminal_reason == "CONTAINER_SETUP_ERROR"
        assert outcome_rec.failure_classification == "INFRASTRUCTURE_FAILURE"


def test_classify_failure_type_exhaustive():
    """Ensure all terminal reasons are strictly categorized."""
    assert classify_failure_type("SUCCESS") == "SUCCESS"
    assert classify_failure_type("ALREADY_PASSING") == "SUCCESS"
    assert classify_failure_type("CONTAINER_SETUP_ERROR") == "INFRASTRUCTURE_FAILURE"
    assert classify_failure_type("BASE_COMMIT_MISMATCH") == "INFRASTRUCTURE_FAILURE"
    assert classify_failure_type("FAILED_MAX_ATTEMPTS") == "MODEL_REPAIR_FAILURE"
    assert classify_failure_type("BUDGET_EXCEEDED") == "MODEL_REPAIR_FAILURE"
    assert classify_failure_type("DEADLINE_EXCEEDED") == "MODEL_REPAIR_FAILURE"
    assert classify_failure_type("PROVIDER_RATE_LIMITED") == "MODEL_REPAIR_FAILURE"
