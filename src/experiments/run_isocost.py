"""Experiment 1: Iso-Cost Strategy Comparison.

Evaluates multiple failure-recovery strategies under a strictly equal dollar budget
per task to eliminate the compute-budget confound.

Strategies evaluated:
1. Single-Shot Large (120B, 1 attempt, burns full budget on 1 shot)
2. Always Repair (20B, up to 4 attempts, repairs with error diagnostics)
3. Always Resample (20B, up to 4 attempts, fresh stochastic sampling)
4. Simple Cascade (20B -> 120B on failure)
5. Retrieve + Repair (20B, retrieves context on failure then repairs)
6. Random Action (20B, uniformly chooses REPAIR, RESAMPLE, or ESCALATE)
"""

import argparse
import csv
import json
import os
import sys
import time
from typing import Dict, Any, List, Optional

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.groq_client import GroqClient
from src.execution_harness import run_problem, ProblemResult
from src.state_builder import ExecutionState
from src.strategies import (
    AlwaysRepairStrategy,
    AlwaysResampleStrategy,
    AlwaysEscalateStrategy,
    RandomActionStrategy,
)


class RetrieveThenRepairStrategy:
    """Hybrid baseline: retrieves context on attempt 1 failure, repairs subsequently."""
    def __init__(self):
        self.name = "retrieve_then_repair"

    def __call__(self, state: ExecutionState) -> str:
        if state.is_terminal:
            return "STOP"
        if state.attempt_number == 1:
            return "RETRIEVE_DOCS"
        return "REPAIR"


def get_completed_task_ids(csv_path: str, strategy_name: str) -> set:
    """Reads completed task IDs from existing CSV to enable crash-safe resumption."""
    if not os.path.exists(csv_path):
        return set()
    completed = set()
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("strategy") == strategy_name and row.get("passed") in ["True", "False"]:
                    completed.add(row["task_id"])
    except Exception:
        pass
    return completed


def run_isocost_experiment(
    tasks_path: str = "data/pilot_tasks.json",
    num_tasks: Optional[int] = None,
    cost_budget_usd: float = 0.003,
    max_attempts: int = 4,
    output_dir: str = "results",
    selected_strategies: Optional[List[str]] = None,
    small_model: str = "openai/gpt-oss-20b",
    large_model: str = "openai/gpt-oss-120b"
) -> Dict[str, Any]:
    """Runs the Iso-Cost comparison across all configured strategies."""
    os.makedirs(output_dir, exist_ok=True)
    attempts_csv = os.path.join(output_dir, "isocost_attempts.csv")
    summary_csv = os.path.join(output_dir, "isocost_summary.csv")

    # Load tasks
    with open(tasks_path, "r", encoding="utf-8") as f:
        tasks = json.load(f)
    if num_tasks:
        tasks = tasks[:num_tasks]

    groq_client = GroqClient()

    # Strategy Configurations
    all_strategies = {
        "single_shot_large": {
            "initial_model": large_model,
            "max_attempts": 1,
            "strategy_fn": lambda s: "STOP",
            "desc": "Single-Shot Large (120B, 1 shot)"
        },
        "always_repair": {
            "initial_model": small_model,
            "max_attempts": max_attempts,
            "strategy_fn": AlwaysRepairStrategy(),
            "desc": "Always Repair (20B, sequential fix)"
        },
        "always_resample": {
            "initial_model": small_model,
            "max_attempts": max_attempts,
            "strategy_fn": AlwaysResampleStrategy(),
            "desc": "Always Resample (20B, fresh seeds)"
        },
        "simple_cascade": {
            "initial_model": small_model,
            "max_attempts": 2,
            "strategy_fn": AlwaysEscalateStrategy(),
            "desc": "Simple Cascade (20B -> 120B)"
        },
        "retrieve_then_repair": {
            "initial_model": small_model,
            "max_attempts": max_attempts,
            "strategy_fn": RetrieveThenRepairStrategy(),
            "desc": "Retrieve + Repair (20B)"
        },
        "random_action": {
            "initial_model": small_model,
            "max_attempts": max_attempts,
            "strategy_fn": RandomActionStrategy(seed=42),
            "desc": "Random Action (20B)"
        }
    }

    if selected_strategies:
        strategies_to_run = {k: v for k, v in all_strategies.items() if k in selected_strategies}
    else:
        strategies_to_run = all_strategies

    print(f"\n{'='*70}")
    print(f"ARGUS EXPERIMENT 1: ISO-COST STRATEGY COMPARISON")
    print(f"Total Tasks: {len(tasks)} | Budget per Task: ${cost_budget_usd:.5f}")
    print(f"Strategies to evaluate: {len(strategies_to_run)}")
    print(f"{'='*70}\n")

    summary_records = []

    for strat_name, strat_cfg in strategies_to_run.items():
        print(f"\n>>> Running Strategy: {strat_cfg['desc']} ({strat_name})")
        results: List[ProblemResult] = []

        for idx, task in enumerate(tasks, 1):
            task_id = task.get("task_id", f"task_{idx}")
            print(f"  [{idx}/{len(tasks)}] Task: {task_id} ...", end=" ", flush=True)

            res = run_problem(
                task=task,
                strategy_fn=strat_cfg["strategy_fn"],
                groq_client=groq_client,
                max_attempts=strat_cfg["max_attempts"],
                cost_budget_usd=cost_budget_usd,
                csv_log_path=attempts_csv,
                small_model=small_model,
                large_model=large_model,
                initial_model=strat_cfg["initial_model"],
                strategy_name=strat_name
            )
            results.append(res)
            status_str = "PASS" if res.passed else f"FAIL ({res.terminal_reason})"
            print(f"{status_str} | Attempts: {res.total_attempts} | Cost: ${res.total_cost_usd:.5f}")

        # Compute summary metrics for this strategy
        total_tasks = len(results)
        solved_tasks = sum(1 for r in results if r.passed)
        pass_rate = (solved_tasks / total_tasks * 100.0) if total_tasks > 0 else 0.0
        total_cost = sum(r.total_cost_usd for r in results)
        avg_cost = (total_cost / total_tasks) if total_tasks > 0 else 0.0
        avg_attempts = (sum(r.total_attempts for r in results) / total_tasks) if total_tasks > 0 else 0.0
        avg_latency = (sum(r.total_latency_ms for r in results) / total_tasks) if total_tasks > 0 else 0.0

        strat_summary = {
            "strategy": strat_name,
            "description": strat_cfg["desc"],
            "tasks_total": total_tasks,
            "tasks_solved": solved_tasks,
            "pass_rate_pct": round(pass_rate, 2),
            "avg_cost_per_task_usd": round(avg_cost, 6),
            "total_cost_usd": round(total_cost, 5),
            "avg_attempts": round(avg_attempts, 2),
            "avg_latency_ms": round(avg_latency, 2),
            "budget_limit_usd": cost_budget_usd
        }
        summary_records.append(strat_summary)

    # Save summary CSV
    summary_fields = [
        "strategy", "description", "tasks_total", "tasks_solved",
        "pass_rate_pct", "avg_cost_per_task_usd", "total_cost_usd",
        "avg_attempts", "avg_latency_ms", "budget_limit_usd"
    ]
    with open(summary_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=summary_fields)
        writer.writeheader()
        writer.writerows(summary_records)

    # Print clean leaderboard table
    print(f"\n{'='*80}")
    print(f"{'STRATEGY':<25} | {'SOLVED':<8} | {'PASS RATE':<10} | {'AVG COST':<12} | {'TOTAL COST':<10} | {'AVG ATTEMPTS':<12}")
    print(f"{'-'*80}")
    for s in summary_records:
        solved_str = f"{s['tasks_solved']}/{s['tasks_total']}"
        pass_str = f"{s['pass_rate_pct']:.1f}%"
        avg_c = f"${s['avg_cost_per_task_usd']:.5f}"
        tot_c = f"${s['total_cost_usd']:.4f}"
        att_str = f"{s['avg_attempts']:.2f}"
        print(f"{s['strategy']:<25} | {solved_str:<8} | {pass_str:<10} | {avg_c:<12} | {tot_c:<10} | {att_str:<12}")
    print(f"{'='*80}\n")
    print(f"[SUCCESS] Iso-cost experiment completed. Results saved to:\n  - {attempts_csv}\n  - {summary_csv}")

    return {"summary": summary_records, "attempts_csv": attempts_csv, "summary_csv": summary_csv}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Argus Experiment 1: Iso-Cost Strategy Comparison")
    parser.add_argument("--tasks", type=str, default="data/pilot_tasks.json", help="Path to tasks JSON file")
    parser.add_argument("--num-tasks", type=int, default=None, help="Number of tasks to evaluate (default: all)")
    parser.add_argument("--budget", type=float, default=0.003, help="Budget per task in USD (default: 0.003)")
    parser.add_argument("--max-attempts", type=int, default=4, help="Max attempts per task (default: 4)")
    parser.add_argument("--output-dir", type=str, default="results", help="Directory for output CSVs")
    parser.add_argument("--strategies", type=str, default=None, help="Comma-separated strategy names to run")

    args = parser.parse_args()
    strat_list = [s.strip() for s in args.strategies.split(",")] if args.strategies else None

    run_isocost_experiment(
        tasks_path=args.tasks,
        num_tasks=args.num_tasks,
        cost_budget_usd=args.budget,
        max_attempts=args.max_attempts,
        output_dir=args.output_dir,
        selected_strategies=strat_list
    )
