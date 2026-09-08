"""Experiment 2: Marginal Return Curves.

Measures the cumulative pass rate and dollar spending across sequential attempts (1..K)
for pure strategies (AlwaysRepair, AlwaysResample, AlwaysEscalate) to empirically locate
the point of diminishing returns.
"""

import argparse
import csv
import json
import os
import sys
from typing import Dict, Any, List, Optional

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.groq_client import GroqClient
from src.execution_harness import run_problem, ProblemResult
from src.strategies import (
    AlwaysRepairStrategy,
    AlwaysResampleStrategy,
    AlwaysEscalateStrategy,
)


def run_marginal_returns_experiment(
    tasks_path: str = "data/pilot_tasks.json",
    num_tasks: Optional[int] = None,
    max_attempts: int = 6,
    cost_budget_usd: float = 0.01,
    output_dir: str = "results",
    small_model: str = "openai/gpt-oss-20b",
    large_model: str = "openai/gpt-oss-120b"
) -> Dict[str, Any]:
    """Runs pure strategies across up to K attempts to map marginal return curves."""
    os.makedirs(output_dir, exist_ok=True)
    attempts_csv = os.path.join(output_dir, "marginal_returns_attempts.csv")
    curve_csv = os.path.join(output_dir, "marginal_returns_curve.csv")

    with open(tasks_path, "r", encoding="utf-8") as f:
        tasks = json.load(f)
    if num_tasks:
        tasks = tasks[:num_tasks]

    groq_client = GroqClient()

    strategies = {
        "always_repair": {
            "initial_model": small_model,
            "strategy_fn": AlwaysRepairStrategy(),
            "desc": "Always Repair (20B)"
        },
        "always_resample": {
            "initial_model": small_model,
            "strategy_fn": AlwaysResampleStrategy(),
            "desc": "Always Resample (20B)"
        },
        "always_escalate": {
            "initial_model": small_model,
            "strategy_fn": AlwaysEscalateStrategy(),
            "desc": "Always Escalate (20B -> 120B)"
        }
    }

    print(f"\n{'='*70}")
    print(f"ARGUS EXPERIMENT 2: MARGINAL RETURN CURVES")
    print(f"Total Tasks: {len(tasks)} | Max Attempts: {max_attempts} | Budget Ceiling: ${cost_budget_usd:.5f}")
    print(f"{'='*70}\n")

    curve_records = []

    for strat_name, strat_cfg in strategies.items():
        print(f"\n>>> Running: {strat_cfg['desc']} ({strat_name})")
        problem_results: List[ProblemResult] = []

        for idx, task in enumerate(tasks, 1):
            task_id = task.get("task_id", f"task_{idx}")
            print(f"  [{idx}/{len(tasks)}] Task: {task_id} ...", end=" ", flush=True)

            res = run_problem(
                task=task,
                strategy_fn=strat_cfg["strategy_fn"],
                groq_client=groq_client,
                max_attempts=max_attempts,
                cost_budget_usd=cost_budget_usd,
                csv_log_path=attempts_csv,
                small_model=small_model,
                large_model=large_model,
                initial_model=strat_cfg["initial_model"],
                strategy_name=strat_name
            )
            problem_results.append(res)
            status_str = "PASS" if res.passed else f"FAIL ({res.terminal_reason})"
            print(f"{status_str} | Attempts: {res.total_attempts} | Cost: ${res.total_cost_usd:.5f}")

        # Calculate Cumulative Marginal Return Curve (Attempts 1 to max_attempts)
        for attempt_k in range(1, max_attempts + 1):
            # A task is solved by attempt k if any attempt <= k passed
            solved_count = 0
            cum_cost_at_k = 0.0

            for pr in problem_results:
                # Sum costs for attempts up to attempt_k
                for a in pr.attempts:
                    if a.attempt_number <= attempt_k:
                        cum_cost_at_k += a.cost_usd
                # Check if passed by attempt_k
                if any(a.passed for a in pr.attempts if a.attempt_number <= attempt_k):
                    solved_count += 1

            cum_pass_rate = (solved_count / len(tasks) * 100.0) if tasks else 0.0

            curve_records.append({
                "strategy": strat_name,
                "attempt_number": attempt_k,
                "cumulative_solved": solved_count,
                "total_tasks": len(tasks),
                "cumulative_pass_rate_pct": round(cum_pass_rate, 2),
                "cumulative_cost_usd": round(cum_cost_at_k, 5),
                "avg_cost_per_task_usd": round(cum_cost_at_k / len(tasks), 6) if tasks else 0.0
            })

    # Save curve CSV
    curve_fields = [
        "strategy", "attempt_number", "cumulative_solved", "total_tasks",
        "cumulative_pass_rate_pct", "cumulative_cost_usd", "avg_cost_per_task_usd"
    ]
    with open(curve_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=curve_fields)
        writer.writeheader()
        writer.writerows(curve_records)

    # Print clean curve table
    print(f"\n{'='*80}")
    print(f"{'STRATEGY':<18} | {'ATTEMPT':<8} | {'SOLVED':<8} | {'PASS RATE':<10} | {'CUMULATIVE COST':<16} | {'AVG COST/TASK':<14}")
    print(f"{'-'*80}")
    for c in curve_records:
        solved_str = f"{c['cumulative_solved']}/{c['total_tasks']}"
        pass_str = f"{c['cumulative_pass_rate_pct']:.1f}%"
        cum_c = f"${c['cumulative_cost_usd']:.4f}"
        avg_c = f"${c['avg_cost_per_task_usd']:.5f}"
        print(f"{c['strategy']:<18} | {c['attempt_number']:<8} | {solved_str:<8} | {pass_str:<10} | {cum_c:<16} | {avg_c:<14}")
    print(f"{'='*80}\n")
    print(f"[SUCCESS] Marginal return experiment completed. Results saved to:\n  - {attempts_csv}\n  - {curve_csv}")

    return {"curve": curve_records, "curve_csv": curve_csv, "attempts_csv": attempts_csv}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Argus Experiment 2: Marginal Return Curves")
    parser.add_argument("--tasks", type=str, default="data/pilot_tasks.json", help="Path to tasks JSON file")
    parser.add_argument("--num-tasks", type=int, default=None, help="Number of tasks to evaluate")
    parser.add_argument("--max-attempts", type=int, default=6, help="Max attempts per task (default: 6)")
    parser.add_argument("--budget", type=float, default=0.01, help="Budget ceiling per task in USD")
    parser.add_argument("--output-dir", type=str, default="results", help="Output directory")

    args = parser.parse_args()
    run_marginal_returns_experiment(
        tasks_path=args.tasks,
        num_tasks=args.num_tasks,
        max_attempts=args.max_attempts,
        cost_budget_usd=args.budget,
        output_dir=args.output_dir
    )
