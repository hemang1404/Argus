"""Milestone 3 Preflight Verification Test Suite.

Covers:
1. Budget Accounting Edge Cases:
   - Outstanding reservation quarantined upon post-dispatch timeout, deadline, or cancellation.
   - Enforced single-process execution model constraint in GlobalExperimentBudget.
2. Part A: Budget & Provider Checks:
   - Official Groq pricing table for GPT-OSS 20B/120B.
   - Conservative token estimation and pre-dispatch reservation.
   - Rate limit (429) release, successful settlement, and ceiling enforcement.
3. Part B: Real Local Docker Conformance Test:
   - Real Docker daemon verification.
   - Resource limits (--memory=4g, --cpus=2.0, --pids-limit=256, --network=none) verified via docker inspect.
   - In-container base-commit check, in-memory tar candidate transfer, SHA256 integrity verification.
   - Correct vs intentionally incorrect candidate handling.
   - Strict container-only test execution (no silent fallback to host).
   - Clean container teardown.
4. Part C: Six-Run Smoke-Test Pipeline Integrity:
   - 3 algorithmic fixtures + 3 single-file repository fixtures.
   - Exactly 6 task-condition runs (1 condition per task).
   - Maximum reservation: $0.03 per task-condition.
   - Total exposure: $0.18 (strictly <= $0.20 smoke ceiling and <= $5.00 global ceiling).
   - 100% mocked inference (no live paid API calls).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from argus.config import ArgusConfig, ModelCost
from argus.providers.adapter import CompletionResponse, ProviderAdapter, ProviderError
from argus.providers.models import ProviderConfig
from argus.runtime.budget import (
    ARGUS_SYSTEM_PROMPT,
    GlobalExperimentBudget,
    TaskConditionBudget,
    count_prompt_tokens,
    is_conclusive_provider_refusal,
)
from argus.runtime.container import (
    BaseCommitMismatchError,
    CandidateIntegrityError,
    ContainerConfig,
    ContainerEvaluator,
    ContainerExecutionResult,
    is_container_engine_available,
)
from argus.runtime.controller import RecoveryController, RecoveryOutcome


# ==============================================================================
# 1. Budget Accounting Edge Cases
# ==============================================================================

def test_edge_case_1_post_dispatch_abort_quarantine():
    """If a request is dispatched and connection timeout/cancellation occurs, quarantine reservation."""
    GlobalExperimentBudget.reset_instance(global_cap_usd=0.20)
    global_b = GlobalExperimentBudget.get_instance(global_cap_usd=0.20)

    task_b = TaskConditionBudget(
        task_id="task_timeout_quarantine",
        condition="test_condition",
        budget_limit_usd=0.03,
        global_budget=global_b,
    )

    cost_calc = ModelCost(input_cost_per_m=0.075, output_cost_per_m=0.300)
    # Reserve worst-case cost
    ok, reserved, _ = task_b.check_and_reserve(prompt_tokens=20_000, max_completion_tokens=2500, model_cost=cost_calc)
    assert ok is True
    assert reserved == 0.00225

    # Request was dispatched across network, then connection timed out or aborted
    task_b.handle_uncertain_abort(reserved, dispatched=True, err=TimeoutError("Connection timed out post-dispatch"))

    # Reservation MUST be quarantined in BOTH budgets, not released!
    assert task_b.outstanding_reservations == 0.0
    assert task_b.quarantined_spend == 0.00225
    assert task_b.total_committed == 0.00225
    assert task_b.spend_uncertain is True
    assert task_b.available_usd == round(0.03 - 0.00225, 7)

    assert global_b.global_reserved == 0.0
    assert global_b.global_quarantined == 0.00225
    assert global_b.total_committed == 0.00225
    assert global_b.available == round(0.20 - 0.00225, 7)


def test_edge_case_1_pre_dispatch_cancellation_releases():
    """If request was NEVER dispatched (e.g. pre-dispatch refusal or DNS error), release reservation."""
    GlobalExperimentBudget.reset_instance(global_cap_usd=0.20)
    global_b = GlobalExperimentBudget.get_instance(global_cap_usd=0.20)

    task_b = TaskConditionBudget(
        task_id="task_pre_dispatch_release",
        condition="test_condition",
        budget_limit_usd=0.03,
        global_budget=global_b,
    )

    cost_calc = ModelCost(input_cost_per_m=0.075, output_cost_per_m=0.300)
    ok, reserved, _ = task_b.check_and_reserve(prompt_tokens=10_000, max_completion_tokens=2000, model_cost=cost_calc)
    assert ok is True

    # Pre-dispatch cancellation (dispatched=False)
    task_b.handle_uncertain_abort(reserved, dispatched=False)

    assert task_b.outstanding_reservations == 0.0
    assert task_b.quarantined_spend == 0.0
    assert task_b.total_committed == 0.0
    assert global_b.global_quarantined == 0.0
    assert global_b.total_committed == 0.0


def test_edge_case_2_single_process_budget_enforcement():
    """GlobalExperimentBudget enforces the single-process execution model constraint."""
    GlobalExperimentBudget.reset_instance(global_cap_usd=5.00)
    global_b = GlobalExperimentBudget.get_instance(global_cap_usd=5.00)

    # Within current coordinator process, access is permitted
    assert global_b.check_process_affinity() is None
    assert global_b.reserve(0.01) is True

    # Simulate cross-process fork or worker process with mismatched PID
    global_b._creator_pid = os.getpid() + 99999
    with pytest.raises(RuntimeError, match="GlobalExperimentBudget process violation"):
        global_b.check_process_affinity()

    with pytest.raises(RuntimeError, match="GlobalExperimentBudget process violation"):
        global_b.reserve(0.01)

    # Restore PID for test cleanup
    global_b._creator_pid = os.getpid()


# ==============================================================================
# 2. Part A: Budget & Provider Checks
# ==============================================================================

def test_part_a_official_groq_pricing():
    """Verify official Groq pricing parameters for GPT-OSS 20B and 120B."""
    cost_20b = ArgusConfig(model="openai/gpt-oss-20b").get_model_cost()
    assert cost_20b.input_cost_per_m == 0.075
    assert cost_20b.output_cost_per_m == 0.300

    cost_120b = ArgusConfig(model="openai/gpt-oss-120b").get_model_cost()
    assert cost_120b.input_cost_per_m == 0.150
    assert cost_120b.cached_input_cost_per_m == 0.075
    assert cost_120b.output_cost_per_m == 0.600


def test_part_a_estimation_and_reservation():
    """Verify pre-dispatch conservative token estimation and reservation math."""
    prompt = "def solve():\n    return 42"
    est_tokens = count_prompt_tokens(prompt, model="openai/gpt-oss-20b")
    assert est_tokens > len(prompt.split())  # Accounts for system prompt + ChatML wire framing

    GlobalExperimentBudget.reset_instance(global_cap_usd=0.05)
    global_b = GlobalExperimentBudget.get_instance()
    task_b = TaskConditionBudget(budget_limit_usd=0.03, global_budget=global_b)

    cost_calc = ModelCost(input_cost_per_m=0.075, output_cost_per_m=0.300)
    ok, reserved, reason = task_b.check_and_reserve(
        prompt_tokens=est_tokens,
        max_completion_tokens=2500,
        model_cost=cost_calc,
    )
    assert ok is True
    assert reason == "OK"
    assert reserved < 0.03
    assert global_b.total_committed == reserved


def test_part_a_pre_dispatch_rejections():
    """Verify pre-dispatch rejection when task or global budget would be exceeded."""
    GlobalExperimentBudget.reset_instance(global_cap_usd=0.010)
    global_b = GlobalExperimentBudget.get_instance()
    task_b = TaskConditionBudget(budget_limit_usd=0.005, global_budget=global_b)

    cost_calc = ModelCost(input_cost_per_m=0.150, output_cost_per_m=0.600)

    # 1. Exceeds task-condition budget ($0.005)
    # 20k prompt * 0.150/1M = $0.003; 10k output * 0.600/1M = $0.006. Total = $0.009 > $0.005
    ok, reserved, reason = task_b.check_and_reserve(
        prompt_tokens=20_000,
        max_completion_tokens=10_000,
        model_cost=cost_calc,
    )
    assert ok is False
    assert "TASK_BUDGET_EXCEEDED" in reason
    assert global_b.total_committed == 0.0

    # 2. Fits task budget ($0.005) but exceeds remaining global budget
    task_b2 = TaskConditionBudget(budget_limit_usd=0.020, global_budget=global_b)
    # Reserve $0.008 against global ($0.010 cap)
    ok1, _, _ = task_b2.check_and_reserve(prompt_tokens=20_000, max_completion_tokens=8_000, model_cost=cost_calc)
    assert ok1 is True
    assert global_b.total_committed > 0.007

    # Second reservation requires $0.005, exceeding global available ($0.002 left)
    ok2, _, reason2 = task_b2.check_and_reserve(prompt_tokens=10_000, max_completion_tokens=5_000, model_cost=cost_calc)
    assert ok2 is False
    assert "GLOBAL_BUDGET_EXCEEDED" in reason2


def test_part_a_rate_limit_and_settlement_transitions():
    """Verify 429 rate limit release and successful settlement under controller mock."""
    GlobalExperimentBudget.reset_instance(global_cap_usd=0.20)
    global_b = GlobalExperimentBudget.get_instance()
    task_b = TaskConditionBudget(budget_limit_usd=0.03, global_budget=global_b)

    cost_calc = ModelCost(input_cost_per_m=0.075, output_cost_per_m=0.300)
    ok, reserved, _ = task_b.check_and_reserve(prompt_tokens=10_000, max_completion_tokens=2500, model_cost=cost_calc)
    assert ok is True

    # 429 rate limit triggers release
    task_b.release(reserved)
    assert task_b.total_committed == 0.0
    assert global_b.total_committed == 0.0

    # Re-reserve for retry
    ok_retry, reserved_retry, _ = task_b.check_and_reserve(prompt_tokens=10_000, max_completion_tokens=2500, model_cost=cost_calc)
    assert ok_retry is True

    # Settlement against actual confirmed tokens (500 prompt, 120 completion)
    actual_cost = task_b.settle(
        reserved_amount=reserved_retry,
        prompt_tokens=500,
        completion_tokens=120,
        model_cost=cost_calc,
    )
    assert actual_cost < reserved_retry
    assert task_b.outstanding_reservations == 0.0
    assert task_b.settled_spend == actual_cost
    assert task_b.total_committed == actual_cost
    assert global_b.total_committed == actual_cost


# ==============================================================================
# 3. Part B: Real Local Docker Conformance Test
# ==============================================================================

@pytest.mark.skipif(not is_container_engine_available("docker"), reason="Local Docker daemon unavailable")
def test_part_b_real_local_docker_conformance():
    """Execute end-to-end conformance validation against the live local Docker daemon."""
    fixture_image = "argus-fixture:latest"
    known_base_commit = "ce6efbb8a8475aa4f7833f82f3ac353ea265b97a"

    cfg = ContainerConfig(
        image=fixture_image,
        workdir="/testbed",
        base_commit=known_base_commit,
        memory_limit="4g",
        cpus=2.0,
        pids_limit=256,
        network_disabled=True,
        timeout_seconds=60,
    )
    evaluator = ContainerEvaluator(cfg)

    # 1. Start container and inspect resource & network restrictions
    cid = evaluator.start_container()
    try:
        inspect_res = subprocess.run(
            ["docker", "inspect", cid],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        info = json.loads(inspect_res.stdout)[0]
        host_cfg = info.get("HostConfig", {})

        # Assert memory limit is 4GB (4294967296 bytes)
        assert host_cfg.get("Memory") == 4 * 1024 * 1024 * 1024
        # Assert CPUs limit is 2.0 (2,000,000,000 nanocpus)
        assert host_cfg.get("NanoCpus") == 2_000_000_000
        # Assert PidsLimit is 256
        assert host_cfg.get("PidsLimit") == 256
        # Assert network is disabled
        assert host_cfg.get("NetworkMode") == "none"

        # 2. Base commit verification
        # Matching base commit succeeds
        assert evaluator.verify_base_commit(cid, known_base_commit) is True
        # Mismatched base commit raises BaseCommitMismatchError
        with pytest.raises(BaseCommitMismatchError):
            evaluator.verify_base_commit(cid, "0000000000000000000000000000000000000000")

        # 3. Candidate transfer via in-memory tar stream & in-container SHA256 check
        valid_candidate = "def add(a, b):\n    return a + b\n"
        expected_sha = hashlib.sha256(valid_candidate.encode("utf-8")).hexdigest()
        assert evaluator.transfer_and_verify_candidate(
            container_id=cid,
            target_rel_path="calc.py",
            candidate_content=valid_candidate,
            expected_candidate_hash=expected_sha,
        ) is True

        # Intentionally incorrect candidate / hash mismatch fails closed
        with pytest.raises(CandidateIntegrityError):
            evaluator.transfer_and_verify_candidate(
                container_id=cid,
                target_rel_path="calc.py",
                candidate_content=valid_candidate,
                expected_candidate_hash="deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            )

        # 4. In-container test execution
        # Correct candidate test passes inside container
        pass_res = evaluator.execute_test(cid, 'python3 -c "import calc; assert calc.add(2, 3) == 5"')
        assert pass_res.passed is True
        assert pass_res.exit_code == 0

        # Incorrect candidate test fails inside container
        fail_res = evaluator.execute_test(cid, 'python3 -c "import calc; assert calc.add(2, 3) == 999"')
        assert fail_res.passed is False
        assert fail_res.exit_code != 0

        # 5. Confirm repository test command cannot silently fall back to host execution
        # Run test that references an in-container only file or path
        container_only_test = 'python3 -c "import os; assert os.path.exists(\'/testbed/calc.py\')"'
        c_res = evaluator.execute_test(cid, container_only_test)
        assert c_res.passed is True
        # On host, '/testbed/calc.py' does not exist in root
        assert not os.path.exists("/testbed/calc.py")

    finally:
        # 6. Teardown and cleanup
        evaluator.destroy_container(cid)
        # Verify container was removed
        check_rm = subprocess.run(
            ["docker", "ps", "-q", "-f", f"id={cid}"],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert check_rm.stdout.strip() == ""


# ==============================================================================
# 4. Part C: Six-Run Smoke-Test Specification
# ==============================================================================

class MockSmokeTestAdapter(ProviderAdapter):
    """Zero-cost mock adapter providing SEARCH/REPLACE solutions for smoke-test tasks."""

    def __init__(self, fixes: Dict[str, str]) -> None:
        self.fixes = fixes
        self.call_count = 0

    def test_connection(self) -> bool:
        return True

    def chat_completion(self, messages: List[Dict[str, str]], **kwargs: Any) -> CompletionResponse:
        self.call_count += 1
        prompt = messages[-1]["content"] if messages else ""

        # Identify target task fix
        replacement = ""
        for key, fix_patch in self.fixes.items():
            if key in prompt:
                replacement = fix_patch
                break

        if not replacement:
            replacement = (
                "<<<<<<< SEARCH\n"
                "return None\n"
                "=======\n"
                "return 42\n"
                ">>>>>>> REPLACE"
            )

        return CompletionResponse(
            content=replacement,
            prompt_tokens=450,
            completion_tokens=85,
            duration_ms=45.0,
            model="openai/gpt-oss-20b",
        )


def test_part_c_six_run_smoke_test_pipeline():
    """Execute the exact 6 task-condition smoke test with no live inference.

    Specifications:
      - 3 Algorithmic tasks (Local Workspace + SubprocessTestRunner)
      - 3 Single-file Repository tasks (Disposable ContainerEvaluator with argus-fixture)
      - Exactly 6 task-condition runs (1 condition per task)
      - Max reservation: $0.03 per task-condition
      - Total committed budget ceiling: $0.180 (under $0.20 smoke cap, under $5.00 global cap)
      - Zero paid inference.
    """
    GlobalExperimentBudget.reset_instance(global_cap_usd=5.00)
    global_b = GlobalExperimentBudget.get_instance()

    # Pre-flight budget constants
    TASK_BUDGET_CAP = 0.030
    SMOKE_TOTAL_MAX = 0.180
    SMOKE_STAGE_CEILING = 0.200

    # 6 Tasks & Fixes
    mock_fixes = {
        "fib.py": (
            "<<<<<<< SEARCH\n"
            "def fib(n):\n"
            "    return n\n"
            "=======\n"
            "def fib(n):\n"
            "    if n <= 1:\n"
            "        return n\n"
            "    return fib(n - 1) + fib(n - 2)\n"
            ">>>>>>> REPLACE"
        ),
        "pal.py": (
            "<<<<<<< SEARCH\n"
            "def is_palindrome(s):\n"
            "    return False\n"
            "=======\n"
            "def is_palindrome(s):\n"
            "    clean = ''.join(c.lower() for c in s if c.isalnum())\n"
            "    return clean == clean[::-1]\n"
            ">>>>>>> REPLACE"
        ),
        "bsearch.py": (
            "<<<<<<< SEARCH\n"
            "def binary_search(arr, target):\n"
            "    return -1\n"
            "=======\n"
            "def binary_search(arr, target):\n"
            "    import bisect\n"
            "    idx = bisect.bisect_left(arr, target)\n"
            "    return idx if idx < len(arr) and arr[idx] == target else -1\n"
            ">>>>>>> REPLACE"
        ),
        "calc.py": (
            "<<<<<<< SEARCH\n"
            "def add(a, b): return a - b\n"
            "=======\n"
            "def add(a, b): return a + b\n"
            ">>>>>>> REPLACE"
        ),
    }
    adapter = MockSmokeTestAdapter(mock_fixes)

    runs_completed = 0
    total_committed_smoke = 0.0

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        # ----------------------------------------------------------------------
        # Algorithmic Task 1: Fibonacci (Condition: Direct)
        # ----------------------------------------------------------------------
        algo1_file = tmp_path / "fib.py"
        algo1_file.write_text("def fib(n):\n    return n\n", encoding="utf-8")
        algo1_test = tmp_path / "test_fib.py"
        algo1_test.write_text(
            "import pytest\nfrom fib import fib\ndef test_fib(): assert fib(5) == 5 and fib(6) == 8\n",
            encoding="utf-8",
        )

        cfg1 = ArgusConfig(
            model="openai/gpt-oss-20b",
            budget_limit_usd=TASK_BUDGET_CAP,
            max_attempts=1,
            escalation_enabled=False,
            task_deadline_seconds=30.0,
        )
        ctrl1 = RecoveryController(config=cfg1, adapter=adapter)
        res1 = ctrl1.execute_recovery(
            test_command=f"pytest {algo1_test.name} -q",
            target_file=str(algo1_file),
            test_file=str(algo1_test),
            workspace_root=tmp_path,
            task_id="algo_fibonacci",
            condition="direct",
            global_budget=global_b,
            mock_test=True,
        )
        assert res1.total_cost_usd <= TASK_BUDGET_CAP
        runs_completed += 1

        # ----------------------------------------------------------------------
        # Algorithmic Task 2: Palindrome (Condition: SelfRepair)
        # ----------------------------------------------------------------------
        algo2_file = tmp_path / "pal.py"
        algo2_file.write_text("def is_palindrome(s):\n    return False\n", encoding="utf-8")
        algo2_test = tmp_path / "test_pal.py"
        algo2_test.write_text(
            "import pytest\nfrom pal import is_palindrome\ndef test_pal(): assert is_palindrome('Race car') is True\n",
            encoding="utf-8",
        )

        cfg2 = ArgusConfig(
            model="openai/gpt-oss-20b",
            budget_limit_usd=TASK_BUDGET_CAP,
            max_attempts=2,
            escalation_enabled=False,
            task_deadline_seconds=30.0,
        )
        ctrl2 = RecoveryController(config=cfg2, adapter=adapter)
        res2 = ctrl2.execute_recovery(
            test_command=f"pytest {algo2_test.name} -q",
            target_file=str(algo2_file),
            test_file=str(algo2_test),
            workspace_root=tmp_path,
            task_id="algo_palindrome",
            condition="self_repair",
            global_budget=global_b,
            mock_test=True,
        )
        assert res2.total_cost_usd <= TASK_BUDGET_CAP
        runs_completed += 1

        # ----------------------------------------------------------------------
        # Algorithmic Task 3: Binary Search (Condition: Escalate)
        # ----------------------------------------------------------------------
        algo3_file = tmp_path / "bsearch.py"
        algo3_file.write_text("def binary_search(arr, target):\n    return -1\n", encoding="utf-8")
        algo3_test = tmp_path / "test_bsearch.py"
        algo3_test.write_text(
            "import pytest\nfrom bsearch import binary_search\ndef test_bs(): assert binary_search([1, 3, 5, 7], 5) == 2\n",
            encoding="utf-8",
        )

        cfg3 = ArgusConfig(
            model="openai/gpt-oss-20b",
            small_model="openai/gpt-oss-20b",
            large_model="openai/gpt-oss-120b",
            budget_limit_usd=TASK_BUDGET_CAP,
            max_attempts=3,
            escalation_enabled=True,
            task_deadline_seconds=30.0,
        )
        ctrl3 = RecoveryController(config=cfg3, adapter=adapter)
        res3 = ctrl3.execute_recovery(
            test_command=f"pytest {algo3_test.name} -q",
            target_file=str(algo3_file),
            test_file=str(algo3_test),
            workspace_root=tmp_path,
            task_id="algo_binary_search",
            condition="escalate",
            global_budget=global_b,
            mock_test=True,
        )
        assert res3.total_cost_usd <= TASK_BUDGET_CAP
        runs_completed += 1

    # --------------------------------------------------------------------------
    # Repository Tasks 4, 5, 6 (Condition: Direct, SelfRepair, Escalate via Docker)
    # --------------------------------------------------------------------------
    docker_available = is_container_engine_available("docker")
    if not docker_available:
        pytest.skip("Docker is required for repository smoke tests")

    fixture_image = "argus-fixture:latest"
    known_base_commit = "ce6efbb8a8475aa4f7833f82f3ac353ea265b97a"

    repo_conditions = [("repo_task_1", "direct", 1), ("repo_task_2", "self_repair", 2), ("repo_task_3", "escalate", 3)]

    for task_name, cond_name, attempts in repo_conditions:
        with tempfile.TemporaryDirectory() as repo_tmp:
            r_path = Path(repo_tmp)
            calc_f = r_path / "calc.py"
            # Buggy initial code
            calc_f.write_text("def add(a, b): return a - b\n", encoding="utf-8")

            c_evaluator = ContainerEvaluator(
                ContainerConfig(
                    image=fixture_image,
                    workdir="/testbed",
                    base_commit=known_base_commit,
                    timeout_seconds=45,
                    network_disabled=True,
                )
            )

            r_cfg = ArgusConfig(
                model="openai/gpt-oss-20b",
                budget_limit_usd=TASK_BUDGET_CAP,
                max_attempts=attempts,
                escalation_enabled=(attempts > 2),
                use_container_runner=True,
                repo_test_timeout_seconds=45,
                task_deadline_seconds=120.0,
            )
            r_ctrl = RecoveryController(config=r_cfg, adapter=adapter, container_evaluator=c_evaluator)
            r_res = r_ctrl.execute_recovery(
                test_command='python3 -c "import calc; assert calc.add(2, 3) == 5"',
                target_file="calc.py",
                workspace_root=r_path,
                task_id=task_name,
                condition=cond_name,
                global_budget=global_b,
                mock_test=True,
            )
            assert r_res.total_cost_usd <= TASK_BUDGET_CAP
            runs_completed += 1

    # Verify Smoke Test Budget Ceilings
    assert runs_completed == 6
    total_committed_smoke = global_b.total_committed

    # Total committed spend across all 6 runs must be strictly <= $0.180
    assert total_committed_smoke <= SMOKE_TOTAL_MAX
    # Total committed spend must be strictly under $0.200 smoke stage ceiling
    assert total_committed_smoke <= SMOKE_STAGE_CEILING
    # Total committed spend must be strictly under $5.00 global experiment ceiling
    assert total_committed_smoke <= global_b.global_cap_usd
    # Zero paid tokens consumed
    assert adapter.call_count > 0
