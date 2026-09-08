"""State Builder for Argus Adaptive Execution Engine.

Constructs structured ExecutionState and AttemptRecord objects that capture
fine-grained failure signatures, test pass rates, token costs, and remaining budgets.
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional
import time


@dataclass
class AttemptRecord:
    """Record of a single generation attempt and its evaluation outcome."""
    attempt_number: int
    action_taken: str                     # "INITIAL", "REPAIR", "RESAMPLE", "ESCALATE", "RETRIEVE"
    model_used: str                       # e.g., "openai/gpt-oss-20b"
    prompt: str
    generated_code: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cumulative_cost_usd: float
    eval_result: Dict[str, Any]
    passed: bool
    tests_total: int
    tests_passed: int
    tests_failed: int
    test_pass_rate: float
    error_type: Optional[str]
    error_msg: str
    failed_test_details: List[Dict[str, str]]
    latency_ms: float
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Converts AttemptRecord to flat dict suitable for CSV logging."""
        return {
            "attempt_number": self.attempt_number,
            "action_taken": self.action_taken,
            "model_used": self.model_used,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "cumulative_cost_usd": self.cumulative_cost_usd,
            "passed": self.passed,
            "tests_total": self.tests_total,
            "tests_passed": self.tests_passed,
            "tests_failed": self.tests_failed,
            "test_pass_rate": self.test_pass_rate,
            "error_type": self.error_type or "none",
            "error_msg": self.error_msg.replace("\n", " ")[:200],
            "latency_ms": self.latency_ms,
            "timestamp": self.timestamp
        }


@dataclass
class ExecutionState:
    """Complete state representation passed to strategy routers at decision time."""
    task_id: str
    source: str
    prompt: str
    entry_point: Optional[str]
    test_code: str
    max_attempts: int
    cost_budget_usd: float
    attempts_history: List[AttemptRecord] = field(default_factory=list)

    # Derived state properties
    attempt_number: int = 0
    cumulative_cost_usd: float = 0.0
    remaining_budget_usd: float = 0.0
    is_terminal: bool = False
    latest_code: str = ""
    latest_eval: Optional[Dict[str, Any]] = None
    error_type: Optional[str] = None
    error_msg: str = ''
    test_pass_rate: float = 0.0
    tests_passed: int = 0
    tests_total: int = 0
    improvement_trend: float = 0.0
    current_model: str = ""

    def to_feature_dict(self) -> Dict[str, Any]:
        """Extracts normalized tabular features for machine-learned routing policies (Phase 6)."""
        error_types = ["syntax_error", "assertion_error", "runtime_error", "timeout", "import_error"]
        one_hot_errors = {f"is_{err}": 1 if self.error_type == err else 0 for err in error_types}
        
        features = {
            "attempt_number": self.attempt_number,
            "test_pass_rate": self.test_pass_rate,
            "tests_passed": self.tests_passed,
            "tests_total": self.tests_total,
            "problem_length_chars": len(self.prompt),
            "code_length_chars": len(self.latest_code),
            "cumulative_cost_usd": self.cumulative_cost_usd,
            "remaining_budget_usd": self.remaining_budget_usd,
            "budget_spent_fraction": (self.cumulative_cost_usd / self.cost_budget_usd) if self.cost_budget_usd > 0 else 1.0,
            "improvement_trend": self.improvement_trend,
            "is_large_model": 1 if "120b" in self.current_model or "70b" in self.current_model else 0,
            **one_hot_errors
        }
        return features


def build_state(
    task: Dict[str, Any],
    attempts_history: List[AttemptRecord],
    cost_budget_usd: float = 0.005,
    max_attempts: int = 5
) -> ExecutionState:
    """Constructs an ExecutionState from the task definition and prior attempt history."""
    task_id = task.get("task_id", "unknown")
    source = task.get("source", "unknown")
    prompt = task.get("prompt", "")
    entry_point = task.get("entry_point")
    test_code = task.get("test_code", "")

    cum_cost = round(sum(a.cost_usd for a in attempts_history), 7)
    rem_budget = round(max(0.0, cost_budget_usd - cum_cost), 7)
    attempt_count = len(attempts_history)

    latest_record = attempts_history[-1] if attempts_history else None
    
    latest_code = latest_record.generated_code if latest_record else ""
    latest_eval = latest_record.eval_result if latest_record else None
    error_type = latest_record.error_type if latest_record else None
    error_msg = latest_record.error_msg if latest_record else ''
    test_pass_rate = latest_record.test_pass_rate if latest_record else 0.0
    tests_passed = latest_record.tests_passed if latest_record else 0
    tests_total = latest_record.tests_total if latest_record else 0
    current_model = latest_record.model_used if latest_record else ""

    # Calculate improvement trend (difference between latest and previous pass rate)
    improvement_trend = 0.0
    if len(attempts_history) >= 2:
        prev_pass_rate = attempts_history[-2].test_pass_rate
        improvement_trend = round(test_pass_rate - prev_pass_rate, 4)

    is_passed = latest_record.passed if latest_record else False
    is_terminal = (
        is_passed or
        attempt_count >= max_attempts or
        rem_budget <= 0.0
    )

    return ExecutionState(
        task_id=task_id,
        source=source,
        prompt=prompt,
        entry_point=entry_point,
        test_code=test_code,
        max_attempts=max_attempts,
        cost_budget_usd=cost_budget_usd,
        attempts_history=attempts_history,
        attempt_number=attempt_count,
        cumulative_cost_usd=cum_cost,
        remaining_budget_usd=rem_budget,
        is_terminal=is_terminal,
        latest_code=latest_code,
        latest_eval=latest_eval,
        error_type=error_type,
        error_msg=error_msg,
        test_pass_rate=test_pass_rate,
        tests_passed=tests_passed,
        tests_total=tests_total,
        improvement_trend=improvement_trend,
        current_model=current_model
    )
