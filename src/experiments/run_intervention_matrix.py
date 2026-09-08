"""Experiment 3: Failure Mode -> Best Intervention Matrix.

For every failed code attempt, evaluates ALL 4 interventions (REPAIR, RESAMPLE,
ESCALATE, RETRIEVE) on that exact same failure state to map which intervention
is empirically optimal for each error taxonomy category.
"""

import argparse
import csv
import json
import os
import sys
from typing import Dict, Any, List, Optional
from collections import defaultdict

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.groq_client import GroqClient
from src.evaluator import evaluate
from src.state_builder import AttemptRecord, ExecutionState, build_state
from src.prompt_builder import format_task_prompt, SYSTEM_PROMPT
from src.actions import (
    RepairAction,
    ResampleAction,
    EscalateAction,
    RetrieveAction,
)


def run_intervention_matrix_experiment(
    tasks_path: str = "data/pilot_tasks.json",
    num_tasks: Optional[int] = None,
    output_dir: str = "results",
    small_model: str = "openai/gpt-oss-20b",
    large_model: str = "openai/gpt-oss-120b"
) -> Dict[str, Any]:
    """Runs a 4-way intervention comparison on every observed failure state."""
    os.makedirs(output_dir, exist_ok=True)
    matrix_csv = os.path.join(output_dir, "intervention_matrix_attempts.csv")
    summary_csv = os.path.join(output_dir, "intervention_matrix_summary.csv")

    with open(tasks_path, "r", encoding="utf-8") as f:
        tasks = json.load(f)
    if num_tasks:
        tasks = tasks[:num_tasks]

    groq_client = GroqClient()

    # Instantiate the 4 interventions
    actions = {
        "REPAIR": RepairAction(model_name=small_model, temperature=0.2),
        "RESAMPLE": ResampleAction(model_name=small_model, temperature=0.7),
        "ESCALATE": EscalateAction(model_name=large_model, temperature=0.2),
        "RETRIEVE": RetrieveAction(model_name=small_model, mode="docs", temperature=0.2),
    }

    # Initialize retriever if available
    retriever = None
    try:
        from src.retrieval import ContextRetriever
        if os.path.exists("data/rag_documents.jsonl"):
            retriever = ContextRetriever()
    except Exception:
        pass

    matrix_rows = []
    error_stats = defaultdict(lambda: {
        "total_failures": 0,
        "repair_passes": 0,
        "resample_passes": 0,
        "escalate_passes": 0,
        "retrieve_passes": 0
    })

    print(f"\n{'='*70}")
    print(f"ARGUS EXPERIMENT 3: FAILURE MODE -> INTERVENTION MATRIX")
    print(f"Total Tasks to Screen: {len(tasks)}")
    print(f"{'='*70}\n")

    failure_count = 0

    for idx, task in enumerate(tasks, 1):
        task_id = task.get("task_id", f"task_{idx}")
        print(f"[{idx}/{len(tasks)}] Generating initial code for: {task_id} ...", end=" ", flush=True)

        # 1. Initial attempt with cheap model
        prompt = format_task_prompt(task)
        initial_res = groq_client.query(prompt, model=small_model, system_prompt=SYSTEM_PROMPT, temperature=0.2)
        initial_code = initial_res.get("content", "")
        eval_res = evaluate(initial_code, task.get("test_code", ""))

        if eval_res["passed"]:
            print("PASS (on shot 1, skipping failure matrix)")
            continue

        failure_count += 1
        error_type = eval_res["error_type"] or "unknown_error"
        print(f"FAILED with {error_type} -> Testing all 4 interventions...")

        # Build initial attempt record and state
        rec = AttemptRecord(
            attempt_number=1,
            action_taken="INITIAL",
            model_used=small_model,
            prompt=prompt,
            generated_code=initial_code,
            input_tokens=initial_res.get("input_tokens", 0),
            output_tokens=initial_res.get("output_tokens", 0),
            cost_usd=initial_res.get("estimated_cost_usd", 0.0),
            cumulative_cost_usd=initial_res.get("estimated_cost_usd", 0.0),
            eval_result=eval_res,
            passed=False,
            tests_total=eval_res["tests_total"],
            tests_passed=eval_res["tests_passed"],
            tests_failed=eval_res["tests_failed"],
            test_pass_rate=eval_res["test_pass_rate"],
            error_type=error_type,
            error_msg=eval_res["error_msg"],
            failed_test_details=eval_res["failed_test_details"],
            latency_ms=initial_res.get("latency_ms", 0.0)
        )
        state = build_state(task, [rec], cost_budget_usd=0.01, max_attempts=5)

        # 2. Test ALL 4 interventions on this exact failure state
        interventions_outcome = {}
        for action_name, action_handler in actions.items():
            action_out = action_handler.execute(
                task=task,
                state=state,
                groq_client=groq_client,
                retriever=retriever
            )
            # Evaluate recovery code
            recovery_eval = evaluate(action_out.generated_code, task.get("test_code", ""))
            interventions_outcome[action_name] = recovery_eval["passed"]

            # Record row
            matrix_rows.append({
                "task_id": task_id,
                "source": task.get("source", "unknown"),
                "failure_error_type": error_type,
                "action_tested": action_name,
                "action_passed": recovery_eval["passed"],
                "action_pass_rate": recovery_eval["test_pass_rate"],
                "action_cost_usd": action_out.cost_usd,
                "action_latency_ms": action_out.latency_ms
            })

        # Update stats
        stats = error_stats[error_type]
        stats["total_failures"] += 1
        if interventions_outcome.get("REPAIR"): stats["repair_passes"] += 1
        if interventions_outcome.get("RESAMPLE"): stats["resample_passes"] += 1
        if interventions_outcome.get("ESCALATE"): stats["escalate_passes"] += 1
        if interventions_outcome.get("RETRIEVE"): stats["retrieve_passes"] += 1

        print(f"    Outcomes: REPAIR={'PASS' if interventions_outcome.get('REPAIR') else 'FAIL'} | "
              f"RESAMPLE={'PASS' if interventions_outcome.get('RESAMPLE') else 'FAIL'} | "
              f"ESCALATE={'PASS' if interventions_outcome.get('ESCALATE') else 'FAIL'} | "
              f"RETRIEVE={'PASS' if interventions_outcome.get('RETRIEVE') else 'FAIL'}")

    # Save detailed attempts CSV
    matrix_fields = [
        "task_id", "source", "failure_error_type", "action_tested",
        "action_passed", "action_pass_rate", "action_cost_usd", "action_latency_ms"
    ]
    with open(matrix_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=matrix_fields)
        writer.writeheader()
        writer.writerows(matrix_rows)

    # Compute summary heatmap rows
    summary_rows = []
    for err_type, stats in sorted(error_stats.items()):
        n = stats["total_failures"]
        summary_rows.append({
            "error_type": err_type,
            "total_failures": n,
            "repair_pass_pct": round((stats["repair_passes"] / n * 100.0), 1) if n > 0 else 0.0,
            "resample_pass_pct": round((stats["resample_passes"] / n * 100.0), 1) if n > 0 else 0.0,
            "escalate_pass_pct": round((stats["escalate_passes"] / n * 100.0), 1) if n > 0 else 0.0,
            "retrieve_pass_pct": round((stats["retrieve_passes"] / n * 100.0), 1) if n > 0 else 0.0,
        })

    summary_fields = [
        "error_type", "total_failures", "repair_pass_pct",
        "resample_pass_pct", "escalate_pass_pct", "retrieve_pass_pct"
    ]
    with open(summary_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=summary_fields)
        writer.writeheader()
        writer.writerows(summary_rows)

    # Print Heatmap Table
    print(f"\n{'='*80}")
    print(f"EMPIRICAL MATRIX: ERROR TYPE -> BEST INTERVENTION SUCCESS RATE")
    print(f"{'='*80}")
    print(f"{'ERROR TYPE':<20} | {'FAILURES':<10} | {'REPAIR':<10} | {'RESAMPLE':<10} | {'ESCALATE':<10} | {'RETRIEVE':<10}")
    print(f"{'-'*80}")
    for r in summary_rows:
        print(f"{r['error_type']:<20} | {r['total_failures']:<10} | {r['repair_pass_pct']:<9}% | {r['resample_pass_pct']:<9}% | {r['escalate_pass_pct']:<9}% | {r['retrieve_pass_pct']:<9}%")
    print(f"{'='*80}\n")
    print(f"[SUCCESS] Intervention matrix experiment completed. Results saved to:\n  - {matrix_csv}\n  - {summary_csv}")

    return {"summary": summary_rows, "matrix_csv": matrix_csv, "summary_csv": summary_csv}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Argus Experiment 3: Failure Mode Matrix")
    parser.add_argument("--tasks", type=str, default="data/pilot_tasks.json", help="Path to tasks JSON file")
    parser.add_argument("--num-tasks", type=int, default=None, help="Number of tasks to evaluate")
    parser.add_argument("--output-dir", type=str, default="results", help="Output directory")

    args = parser.parse_args()
    run_intervention_matrix_experiment(
        tasks_path=args.tasks,
        num_tasks=args.num_tasks,
        output_dir=args.output_dir
    )
