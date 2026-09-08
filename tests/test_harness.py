"""Unit tests for src/state_builder.py and src/execution_harness.py."""

import os
import sys
import tempfile
import csv

# Add root directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.state_builder import AttemptRecord, ExecutionState, build_state
from src.execution_harness import run_problem, ProblemResult


class MockGroqClient:
    """Mock client for deterministic harness testing without network or cost."""
    def __init__(self, responses=None):
        self.responses = responses or []
        self.call_count = 0

    def query(self, prompt, model="openai/gpt-oss-20b", system_prompt=None, temperature=0.2):
        self.call_count += 1
        if self.responses and len(self.responses) >= self.call_count:
            content = self.responses[self.call_count - 1]
        else:
            content = "def add(a, b):\n    return a + b"
            
        return {
            "success": True,
            "content": content,
            "input_tokens": 120,
            "output_tokens": 40,
            "latency_ms": 15.0,
            "estimated_cost_usd": 0.0001,
            "error": None
        }


def test_state_builder():
    print("=== Testing State Builder ===")
    task = {
        "task_id": "test_1",
        "source": "TestBench",
        "prompt": "def solve(): pass",
        "test_code": "assert solve() == 1"
    }
    
    # 1. Zero attempts state
    state_0 = build_state(task, [], cost_budget_usd=0.005, max_attempts=3)
    assert state_0.attempt_number == 0
    assert state_0.cumulative_cost_usd == 0.0
    assert state_0.remaining_budget_usd == 0.005
    assert state_0.is_terminal is False
    print("State 0 (Initial): OK")

    # 2. Add an attempt record
    rec1 = AttemptRecord(
        attempt_number=1,
        action_taken="INITIAL",
        model_used="openai/gpt-oss-20b",
        prompt="...",
        generated_code="def solve(): return 0",
        input_tokens=100,
        output_tokens=30,
        cost_usd=0.0001,
        cumulative_cost_usd=0.0001,
        eval_result={"passed": False, "tests_total": 2, "tests_passed": 1, "tests_failed": 1, "test_pass_rate": 0.5, "error_type": "assertion_error", "error_msg": "Assertion failed", "failed_test_details": [], "execution_time_s": 0.02},
        passed=False,
        tests_total=2,
        tests_passed=1,
        tests_failed=1,
        test_pass_rate=0.5,
        error_type="assertion_error",
        error_msg="Assertion failed",
        failed_test_details=[],
        latency_ms=20.0
    )
    
    state_1 = build_state(task, [rec1], cost_budget_usd=0.005, max_attempts=3)
    assert state_1.attempt_number == 1
    assert state_1.cumulative_cost_usd == 0.0001
    assert state_1.remaining_budget_usd == 0.0049
    assert state_1.test_pass_rate == 0.5
    assert state_1.improvement_trend == 0.0
    print("State 1 (Single Failure): OK")

    # 3. Add second attempt with improved pass rate
    rec2 = AttemptRecord(
        attempt_number=2,
        action_taken="REPAIR",
        model_used="openai/gpt-oss-20b",
        prompt="...",
        generated_code="def solve(): return 1",
        input_tokens=150,
        output_tokens=30,
        cost_usd=0.0001,
        cumulative_cost_usd=0.0002,
        eval_result={"passed": True, "tests_total": 2, "tests_passed": 2, "tests_failed": 0, "test_pass_rate": 1.0, "error_type": None, "error_msg": "", "failed_test_details": [], "execution_time_s": 0.02},
        passed=True,
        tests_total=2,
        tests_passed=2,
        tests_failed=0,
        test_pass_rate=1.0,
        error_type=None,
        error_msg="",
        failed_test_details=[],
        latency_ms=25.0
    )
    
    state_2 = build_state(task, [rec1, rec2], cost_budget_usd=0.005, max_attempts=3)
    assert state_2.attempt_number == 2
    assert state_2.test_pass_rate == 1.0
    assert state_2.improvement_trend == 0.5  # 1.0 - 0.5
    assert state_2.is_terminal is True       # because passed is True
    
    # Check feature dictionary
    feats = state_2.to_feature_dict()
    assert feats["attempt_number"] == 2
    assert feats["test_pass_rate"] == 1.0
    assert feats["improvement_trend"] == 0.5
    assert feats["is_assertion_error"] == 0
    print("State 2 (Terminal Pass + Feature Dict): OK")


def test_harness_loop():
    print("\n=== Testing Execution Harness Loop ===")
    task = {
        "task_id": "math_add",
        "source": "HumanEval",
        "prompt": "def add(a, b):",
        "entry_point": "add",
        "test_code": "assert add(2, 3) == 5\nassert add(1, 1) == 2"
    }

    # Case A: Trivial STOP strategy (Single-shot)
    client_a = MockGroqClient(responses=["def add(a, b): return a - b"])  # Buggy code
    def stop_strategy(state): return "STOP"
    
    res_a = run_problem(task, stop_strategy, client_a, max_attempts=4)
    assert res_a.total_attempts == 1
    assert res_a.passed is False
    assert res_a.terminal_reason == "stopped_by_policy"
    print("Case A (Single-shot STOP): OK")

    # Case B: Multi-attempt REPAIR (Attempt 1 fails, Attempt 2 fixes)
    client_b = MockGroqClient(responses=[
        "def add(a, b): return a - b",  # Attempt 1: Buggy
        "def add(a, b): return a + b"   # Attempt 2: Correct!
    ])
    def repair_strategy(state): return "REPAIR"
    
    temp_csv = tempfile.NamedTemporaryFile(suffix=".csv", delete=False).name
    try:
        res_b = run_problem(
            task,
            repair_strategy,
            client_b,
            max_attempts=4,
            csv_log_path=temp_csv
        )
        assert res_b.total_attempts == 2
        assert res_b.passed is True
        assert res_b.terminal_reason == "passed"
        assert res_b.final_pass_rate == 1.0
        
        # Verify CSV contents
        with open(temp_csv, "r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            assert len(reader) == 2
            assert reader[0]["attempt_number"] == "1"
            assert reader[0]["action_taken"] == "INITIAL"
            assert reader[0]["passed"] == "False"
            assert reader[1]["attempt_number"] == "2"
            assert reader[1]["action_taken"] == "REPAIR"
            assert reader[1]["passed"] == "True"
        print("Case B (Repair Loop + Crash-safe CSV): OK")
    finally:
        if os.path.exists(temp_csv):
            os.remove(temp_csv)

    # Case C: Budget Exhaustion
    client_c = MockGroqClient(responses=["def add(a, b): return 0"] * 5)
    res_c = run_problem(
        task,
        repair_strategy,
        client_c,
        max_attempts=10,
        cost_budget_usd=0.0001  # Only enough for 1 attempt (each costs 0.0001)
    )
    assert res_c.total_attempts == 1
    assert res_c.terminal_reason == "budget_exhausted"
    print("Case C (Budget Exhaustion Enforcement): OK")

    # Case D: Max Attempts Reached
    client_d = MockGroqClient(responses=["def add(a, b): return 0"] * 5)
    res_d = run_problem(
        task,
        repair_strategy,
        client_d,
        max_attempts=3,
        cost_budget_usd=0.01
    )
    assert res_d.total_attempts == 3
    assert res_d.terminal_reason == "max_attempts_reached"
    print("Case D (Max Attempts Ceiling): OK")

    print("\n[SUCCESS] All State Builder and Execution Harness tests passed!")


if __name__ == "__main__":
    test_state_builder()
    test_harness_loop()
