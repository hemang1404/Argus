"""Unit tests for src/actions and src/strategies."""

import os
import sys

# Add root directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.actions import (
    RepairAction,
    ResampleAction,
    EscalateAction,
    RetrieveAction,
    ActionResult
)
from src.strategies import (
    AlwaysRepairStrategy,
    AlwaysResampleStrategy,
    AlwaysEscalateStrategy,
    RandomActionStrategy
)
from src.state_builder import AttemptRecord, ExecutionState, build_state
from src.execution_harness import run_problem


class SpyGroqClient:
    """Mock client that records the arguments it received."""
    def __init__(self, return_code="def solve(): return 42"):
        self.return_code = return_code
        self.last_prompt = ""
        self.last_model = ""
        self.last_temperature = 0.0

    def query(self, prompt, model="openai/gpt-oss-20b", system_prompt=None, temperature=0.2):
        self.last_prompt = prompt
        self.last_model = model
        self.last_temperature = temperature
        return {
            "success": True,
            "content": self.return_code,
            "input_tokens": 150,
            "output_tokens": 30,
            "latency_ms": 25.0,
            "estimated_cost_usd": 0.0001,
            "error": None
        }


class MockRetriever:
    """Mock retriever returning static doc chunks and few-shot exemplars."""
    def retrieve_rag_docs(self, query, k=2):
        return [
            {"topic": "Python Math", "content": "math.gcd(a, b) returns greatest common divisor."},
            {"topic": "Recursion", "content": "Base case terminates recursive stack."}
        ], 5.0

    def retrieve_few_shot(self, query, k=2):
        return [
            {"id": "ex_1", "problem": "def solve(n):", "solution": "return n * n"}
        ], 4.0


def make_sample_state():
    task = {
        "task_id": "sample_task",
        "source": "HumanEval",
        "prompt": "def solve(n):",
        "entry_point": "solve",
        "test_code": "assert solve(2) == 4\nassert solve(3) == 9"
    }
    rec = AttemptRecord(
        attempt_number=1,
        action_taken="INITIAL",
        model_used="openai/gpt-oss-20b",
        prompt="def solve(n):",
        generated_code="def solve(n): return n + 2",
        input_tokens=100,
        output_tokens=30,
        cost_usd=0.0001,
        cumulative_cost_usd=0.0001,
        eval_result={
            "passed": False,
            "tests_total": 2,
            "tests_passed": 1,
            "tests_failed": 1,
            "test_pass_rate": 0.5,
            "error_type": "assertion_error",
            "error_msg": "Assertion failed: assert solve(3) == 9",
            "failed_test_details": [
                {"test_line": "assert solve(3) == 9", "error": "Assertion failed: assert solve(3) == 9"}
            ],
            "execution_time_s": 0.02
        },
        passed=False,
        tests_total=2,
        tests_passed=1,
        tests_failed=1,
        test_pass_rate=0.5,
        error_type="assertion_error",
        error_msg="Assertion failed: assert solve(3) == 9",
        failed_test_details=[
            {"test_line": "assert solve(3) == 9", "error": "Assertion failed: assert solve(3) == 9"}
        ],
        latency_ms=20.0
    )
    state = build_state(task, [rec], cost_budget_usd=0.005, max_attempts=4)
    return task, state


def test_repair_action():
    print("=== Testing RepairAction ===")
    task, state = make_sample_state()
    repair = RepairAction()
    client = SpyGroqClient()

    res = repair.execute(task, state, client)
    assert res.action_name == "REPAIR"
    assert "assert solve(3) == 9" in client.last_prompt
    assert "def solve(n): return n + 2" in client.last_prompt
    assert client.last_temperature == 0.2
    assert client.last_model == "openai/gpt-oss-20b"
    print("RepairAction: OK")


def test_resample_action():
    print("=== Testing ResampleAction ===")
    task, state = make_sample_state()
    resample = ResampleAction(temperature=0.7)
    client = SpyGroqClient()

    res = resample.execute(task, state, client)
    assert res.action_name == "RESAMPLE"
    # Verify broken code is NOT present in resample prompt (unanchored)
    assert "def solve(n): return n + 2" not in client.last_prompt
    assert client.last_temperature == 0.7
    print("ResampleAction: OK")


def test_escalate_action():
    print("=== Testing EscalateAction ===")
    task, state = make_sample_state()
    escalate = EscalateAction(model_name="openai/gpt-oss-120b")
    client = SpyGroqClient()

    res = escalate.execute(task, state, client)
    assert res.action_name == "ESCALATE"
    assert client.last_model == "openai/gpt-oss-120b"
    assert client.last_temperature == 0.2
    print("EscalateAction: OK")


def test_retrieve_action():
    print("=== Testing RetrieveAction (Docs, Few-Shot, Hybrid) ===")
    task, state = make_sample_state()
    client = SpyGroqClient()
    retriever = MockRetriever()

    # 1. Mode: docs (RAG)
    r_docs = RetrieveAction(mode="docs")
    res_docs = r_docs.execute(task, state, client, retriever=retriever)
    assert res_docs.action_name == "RETRIEVE"
    assert "Relevant Technical Documentation" in client.last_prompt
    assert "math.gcd" in client.last_prompt
    assert "Reference Code Examples" not in client.last_prompt

    # 2. Mode: few_shot (Exemplars)
    r_fs = RetrieveAction(mode="few_shot")
    res_fs = r_fs.execute(task, state, client, retriever=retriever)
    assert res_fs.action_name == "RETRIEVE_FEW_SHOT"
    assert "Reference Code Examples" in client.last_prompt
    assert "return n * n" in client.last_prompt
    assert "Relevant Technical Documentation" not in client.last_prompt

    # 3. Mode: hybrid (Both)
    r_hyb = RetrieveAction(mode="hybrid")
    res_hyb = r_hyb.execute(task, state, client, retriever=retriever)
    assert res_hyb.action_name == "RETRIEVE_HYBRID"
    assert "Relevant Technical Documentation" in client.last_prompt
    assert "Reference Code Examples" in client.last_prompt

    # 4. Fallback without retriever
    res_fallback = r_docs.execute(task, state, client, retriever=None)
    assert res_fallback.action_name == "RETRIEVE"
    print("RetrieveAction (Docs, Few-Shot, Hybrid): OK")


def test_fixed_strategies():
    print("=== Testing Fixed Strategies ===")
    task, state = make_sample_state()

    s_repair = AlwaysRepairStrategy()
    s_resample = AlwaysResampleStrategy()
    s_escalate = AlwaysEscalateStrategy()
    s_random = RandomActionStrategy(seed=42)

    assert s_repair(state) == "REPAIR"
    assert s_resample(state) == "RESAMPLE"
    assert s_escalate(state) == "ESCALATE"
    assert s_random(state) in ["REPAIR", "RESAMPLE", "ESCALATE"]

    # When state is terminal, all strategies should return STOP
    terminal_state = build_state(task, state.attempts_history, cost_budget_usd=0.0001)
    assert terminal_state.is_terminal is True
    assert s_repair(terminal_state) == "STOP"
    assert s_resample(terminal_state) == "STOP"
    assert s_escalate(terminal_state) == "STOP"
    assert s_random(terminal_state) == "STOP"
    print("Fixed Strategies: OK")


def test_harness_integration_with_actions():
    print("=== Testing Harness Integration with New Actions ===")
    task, _ = make_sample_state()
    client = SpyGroqClient(return_code="def solve(n):\n    return n * n")  # Solves the problem
    
    # Test harness running with AlwaysEscalateStrategy
    res = run_problem(
        task=task,
        strategy_fn=AlwaysEscalateStrategy(),
        groq_client=client,
        max_attempts=3
    )
    assert res.passed is True
    print("Harness Integration with AlwaysEscalate: OK")

    print("\n[SUCCESS] All Action and Strategy tests passed flawlessly!")


if __name__ == "__main__":
    test_repair_action()
    test_resample_action()
    test_escalate_action()
    test_retrieve_action()
    test_fixed_strategies()
    test_harness_integration_with_actions()
