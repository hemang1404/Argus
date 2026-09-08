"""Execution Harness for Argus Iterative Inference Allocation.

Runs coding problems through a multi-attempt loop guided by pluggable strategy functions,
tracking cumulative token cost, remaining budget, and logging each attempt immediately
to disk for crash-safe execution.
"""

import csv
import os
import time
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional, Callable, Tuple

from src.evaluator import evaluate, sanitize_generated_code
from src.state_builder import AttemptRecord, ExecutionState, build_state
from src.prompt_builder import format_task_prompt, SYSTEM_PROMPT
from src.actions import (
    BaseAction,
    ActionResult,
    RepairAction,
    ResampleAction,
    EscalateAction,
    RetrieveAction
)


@dataclass
class ProblemResult:
    """Consolidated summary of a problem solved or attempted through the harness."""
    task_id: str
    source: str
    passed: bool
    total_attempts: int
    terminal_reason: str                   # "passed", "max_attempts_reached", "budget_exhausted", "stopped_by_policy"
    total_cost_usd: float
    total_input_tokens: int
    total_output_tokens: int
    final_pass_rate: float
    best_pass_rate: float
    total_latency_ms: float
    attempts: List[AttemptRecord]

    def to_dict(self) -> Dict[str, Any]:
        """Converts ProblemResult to summary dictionary."""
        return {
            "task_id": self.task_id,
            "source": self.source,
            "passed": self.passed,
            "total_attempts": self.total_attempts,
            "terminal_reason": self.terminal_reason,
            "total_cost_usd": self.total_cost_usd,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "final_pass_rate": self.final_pass_rate,
            "best_pass_rate": self.best_pass_rate,
            "total_latency_ms": self.total_latency_ms
        }


CSV_FIELDNAMES = [
    "task_id", "source", "attempt_number", "action_taken", "model_used",
    "passed", "tests_total", "tests_passed", "tests_failed", "test_pass_rate",
    "error_type", "input_tokens", "output_tokens", "cost_usd",
    "cumulative_cost_usd", "remaining_budget_usd", "latency_ms", "timestamp"
]


def log_attempt_to_csv(csv_path: str, record: AttemptRecord, task_id: str, source: str, remaining_budget: float) -> None:
    """Appends an AttemptRecord to CSV immediately and flushes disk buffer for crash-safety."""
    file_has_header = os.path.exists(csv_path) and os.path.getsize(csv_path) > 0
    os.makedirs(os.path.dirname(os.path.abspath(csv_path)), exist_ok=True)
    
    with open(csv_path, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        if not file_has_header:
            writer.writeheader()
        row = record.to_dict()
        row["task_id"] = task_id
        row["source"] = source
        row["remaining_budget_usd"] = remaining_budget
        writer.writerow({k: row.get(k, "") for k in CSV_FIELDNAMES})
        f.flush()


# Default action handlers instantiated once
ACTION_REGISTRY: Dict[str, BaseAction] = {
    "REPAIR": RepairAction(),
    "RESAMPLE": ResampleAction(),
    "ESCALATE": EscalateAction(),
    "RETRIEVE": RetrieveAction(mode="docs"),
    "RETRIEVE_DOCS": RetrieveAction(mode="docs"),
    "RETRIEVE_FEW_SHOT": RetrieveAction(mode="few_shot"),
    "RETRIEVE_HYBRID": RetrieveAction(mode="hybrid"),
}


def execute_action(
    action: str,
    task: Dict[str, Any],
    state: ExecutionState,
    groq_client: Any,
    small_model: str,
    large_model: str,
    retriever: Optional[Any] = None
) -> ActionResult:
    """Dispatches execution to the appropriate BaseAction module."""
    handler = ACTION_REGISTRY.get(action, ACTION_REGISTRY["RESAMPLE"])
    target_model = large_model if action == "ESCALATE" else small_model
    return handler.execute(
        task=task,
        state=state,
        groq_client=groq_client,
        model=target_model,
        retriever=retriever
    )


def run_problem(
    task: Dict[str, Any],
    strategy_fn: Callable[[ExecutionState], str],
    groq_client: Any,
    evaluator_fn: Callable[[str, str], Dict[str, Any]] = evaluate,
    max_attempts: int = 5,
    cost_budget_usd: float = 0.005,
    csv_log_path: Optional[str] = None,
    small_model: str = "openai/gpt-oss-20b",
    large_model: str = "openai/gpt-oss-120b",
    retriever: Optional[Any] = None
) -> ProblemResult:
    """Runs a single coding problem through the iterative inference loop.
    
    Args:
        task: Problem specification dictionary.
        strategy_fn: Callable taking ExecutionState and returning next action ("REPAIR", "RESAMPLE", "ESCALATE", "STOP").
        groq_client: Client for LLM generation.
        evaluator_fn: Function to evaluate code against tests.
        max_attempts: Maximum attempts before giving up.
        cost_budget_usd: Maximum dollar budget to spend on this problem.
        csv_log_path: Filepath for immediate attempt logging.
        small_model: Cheap model ID.
        large_model: Frontier model ID.
        retriever: Optional context retriever for RAG.
        
    Returns:
        ProblemResult with complete attempt telemetry.
    """
    task_id = task.get("task_id", "unknown")
    source = task.get("source", "unknown")
    attempts_history: List[AttemptRecord] = []
    
    terminal_reason = "max_attempts_reached"
    
    while len(attempts_history) < max_attempts:
        current_attempt = len(attempts_history) + 1
        
        # Build state so far
        state = build_state(task, attempts_history, cost_budget_usd, max_attempts)
        
        # Check budget before launching call
        if state.remaining_budget_usd <= 0.0:
            terminal_reason = "budget_exhausted"
            break
            
        # Determine action
        if current_attempt == 1:
            action = "INITIAL"
            prompt = format_task_prompt(task)
            model = small_model
            query_res = groq_client.query(prompt, model=model, system_prompt=SYSTEM_PROMPT, temperature=0.2)
            code = query_res.get("content", "")
            cost_usd = query_res.get("estimated_cost_usd", 0.0)
            in_tok = query_res.get("input_tokens", 0)
            out_tok = query_res.get("output_tokens", 0)
            lat_ms = query_res.get("latency_ms", 0.0)
        else:
            action = strategy_fn(state)
            if action == "STOP":
                terminal_reason = "stopped_by_policy"
                break
            action_res = execute_action(
                action, task, state, groq_client, small_model, large_model, retriever
            )
            code = action_res.generated_code
            model = action_res.model_used
            prompt = action_res.prompt
            cost_usd = action_res.cost_usd
            in_tok = action_res.input_tokens
            out_tok = action_res.output_tokens
            lat_ms = action_res.latency_ms
            
        cum_cost = round(sum(a.cost_usd for a in attempts_history) + cost_usd, 7)
        rem_budget = round(max(0.0, cost_budget_usd - cum_cost), 7)
        
        # Evaluate execution
        eval_res = evaluator_fn(code, task.get("test_code", ""))
        
        # Build attempt record
        record = AttemptRecord(
            attempt_number=current_attempt,
            action_taken=action,
            model_used=model,
            prompt=prompt,
            generated_code=code,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost_usd,
            cumulative_cost_usd=cum_cost,
            eval_result=eval_res,
            passed=eval_res["passed"],
            tests_total=eval_res["tests_total"],
            tests_passed=eval_res["tests_passed"],
            tests_failed=eval_res["tests_failed"],
            test_pass_rate=eval_res["test_pass_rate"],
            error_type=eval_res["error_type"],
            error_msg=eval_res["error_msg"],
            failed_test_details=eval_res["failed_test_details"],
            latency_ms=lat_ms
        )
        attempts_history.append(record)
        
        # Crash-safe flush to CSV
        if csv_log_path:
            log_attempt_to_csv(csv_log_path, record, task_id, source, rem_budget)
            
        # Terminal check: did all tests pass?
        if eval_res["passed"]:
            terminal_reason = "passed"
            break
            
        if rem_budget <= 0.0:
            terminal_reason = "budget_exhausted"
            break
            
    # Calculate summary metrics
    total_cost = round(sum(a.cost_usd for a in attempts_history), 7)
    total_in = sum(a.input_tokens for a in attempts_history)
    total_out = sum(a.output_tokens for a in attempts_history)
    total_lat = round(sum(a.latency_ms for a in attempts_history), 2)
    passed_overall = any(a.passed for a in attempts_history)
    final_pass_rate = attempts_history[-1].test_pass_rate if attempts_history else 0.0
    best_pass_rate = max((a.test_pass_rate for a in attempts_history), default=0.0)
    
    return ProblemResult(
        task_id=task_id,
        source=source,
        passed=passed_overall,
        total_attempts=len(attempts_history),
        terminal_reason=terminal_reason,
        total_cost_usd=total_cost,
        total_input_tokens=total_in,
        total_output_tokens=total_out,
        final_pass_rate=final_pass_rate,
        best_pass_rate=best_pass_rate,
        total_latency_ms=total_lat,
        attempts=attempts_history
    )
