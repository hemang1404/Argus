"""Phase A Screening Runner: Zero-shot (S0) Baseline Evaluation.

Executes candidate tasks across small and large models for N runs,
logging token consumption, latency, costs, and evaluation results incrementally.
"""

import os
import sys
import csv
import time
import argparse
import yaml

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tqdm import tqdm
from typing import Set, Tuple, List, Dict, Any

from src.groq_client import GroqClient
from src.evaluator import evaluate
from src.task_loader import load_candidate_tasks


SYSTEM_PROMPT = "You are an expert Python programming assistant. Write correct, self-contained Python code. Return ONLY valid Python code, no explanations, no markdown."


def format_s0_prompt(task: Dict[str, Any]) -> str:
    """Constructs the S0 (Zero-shot Baseline) prompt."""
    prompt_text = task["prompt"]
    entry_point = task.get("entry_point")
    
    if task["source"] == "MBPP":
        return (
            f"Problem:\n{prompt_text}\n\n"
            f"Your function must be named `{entry_point}`.\n"
            f"Return ONLY the executable Python code with no markdown formatting."
        )
    else:
        # HumanEval prompt already includes signature + docstrings
        return (
            f"{prompt_text}\n\n"
            f"Return ONLY the completed Python function code with no markdown formatting."
        )


def load_completed_runs(csv_path: str) -> Set[Tuple[str, str, int]]:
    """Loads set of (task_id, model, run_number) tuples that are already finished."""
    completed = set()
    if not os.path.exists(csv_path):
        return completed
        
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                completed.add((row["task_id"], row["model"], int(row["run_number"])))
    except Exception as e:
        print(f"[WARN] Error reading existing CSV ({e}), starting fresh check.")
    return completed


def run_screening_experiment(
    config_path: str = "configs/experiment.yaml",
    tasks_path: str = "data/candidate_tasks.jsonl",
    output_csv: str = "results/baseline_screening.csv",
    task_limit: int = 0,
    model_override: str = "",
    runs_override: int = 0,
    delay_between_calls: float = 0.5
) -> None:
    """Runs the Phase A baseline screening experiment."""
    # 1. Load Experiment Config
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
        
    if model_override:
        models = [model_override]
    else:
        models = [config["models"]["small"], config["models"]["large"]]
        
    runs_per_config = runs_override if runs_override > 0 else config["generation"].get("runs_per_config", 3)
    temperature = config["generation"].get("temperature", 0.2)
    max_tokens = config["generation"].get("max_tokens", 512)
    timeout_sec = config["evaluation"].get("timeout_seconds", 5)

    # 2. Load Tasks
    tasks = load_candidate_tasks(tasks_path)
    if task_limit > 0:
        tasks = tasks[:task_limit]
        print(f"[*] Limited to first {task_limit} tasks for testing.")
        
    print(f"[*] Loaded {len(tasks)} tasks.")
    print(f"[*] Models to evaluate: {models}")
    print(f"[*] Runs per task: {runs_per_config}")
    total_expected = len(tasks) * len(models) * runs_per_config
    print(f"[*] Total API calls scheduled: {total_expected}")

    # 3. Setup CSV & Resume Tracking
    os.makedirs(os.path.dirname(output_csv), exist_ok=True)
    completed_runs = load_completed_runs(output_csv)
    print(f"[*] Found {len(completed_runs)} already completed runs.")

    fieldnames = [
        "task_id", "source", "model", "strategy", "run_number",
        "passed", "error_type", "input_tokens", "output_tokens",
        "latency_ms", "estimated_cost_usd", "execution_time_s", "error_msg"
    ]
    
    file_exists = os.path.exists(output_csv)
    csv_file = open(output_csv, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
    
    if not file_exists or os.path.getsize(output_csv) == 0:
        writer.writeheader()
        csv_file.flush()

    # 4. Initialize Groq Client
    client = GroqClient()
    
    # 5. Execution Loop
    progress_bar = tqdm(total=total_expected, initial=len(completed_runs), desc="Baseline Screening")
    
    try:
        for model in models:
            for run_num in range(1, runs_per_config + 1):
                for task in tasks:
                    task_id = task["task_id"]
                    
                    # Skip if already completed
                    if (task_id, model, run_num) in completed_runs:
                        continue
                        
                    user_prompt = format_s0_prompt(task)
                    
                    # Query Groq
                    query_res = client.query(
                        prompt=user_prompt,
                        model=model,
                        system_prompt=SYSTEM_PROMPT,
                        temperature=temperature,
                        max_tokens=max_tokens
                    )
                    
                    if not query_res["success"]:
                        # API failure row
                        row = {
                            "task_id": task_id,
                            "source": task["source"],
                            "model": model,
                            "strategy": "S0",
                            "run_number": run_num,
                            "passed": False,
                            "error_type": "api_error",
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "latency_ms": query_res["latency_ms"],
                            "estimated_cost_usd": 0.0,
                            "execution_time_s": 0.0,
                            "error_msg": query_res["error"] or "API call failed"
                        }
                    else:
                        # Evaluate generated code in subprocess
                        eval_res = evaluate(
                            generated_code=query_res["content"],
                            test_code=task["test_code"],
                            timeout_seconds=timeout_sec
                        )
                        
                        row = {
                            "task_id": task_id,
                            "source": task["source"],
                            "model": model,
                            "strategy": "S0",
                            "run_number": run_num,
                            "passed": eval_res["passed"],
                            "error_type": eval_res["error_type"] or "",
                            "input_tokens": query_res["input_tokens"],
                            "output_tokens": query_res["output_tokens"],
                            "latency_ms": query_res["latency_ms"],
                            "estimated_cost_usd": query_res["estimated_cost_usd"],
                            "execution_time_s": eval_res["execution_time_s"],
                            "error_msg": eval_res["error_msg"][:200]  # Store first 200 chars of error
                        }

                    # Write immediately to disk
                    writer.writerow(row)
                    csv_file.flush()
                    
                    completed_runs.add((task_id, model, run_num))
                    progress_bar.update(1)
                    
                    if delay_between_calls > 0:
                        time.sleep(delay_between_calls)

    finally:
        csv_file.close()
        progress_bar.close()
        print(f"[SUCCESS] Screening complete. Results saved to {output_csv}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Phase A baseline screening on benchmark tasks.")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of tasks to run (0 for all).")
    parser.add_argument("--model", type=str, default="", help="Specific model to run (e.g. openai/gpt-oss-120b).")
    parser.add_argument("--runs", type=int, default=0, help="Number of runs per task.")
    parser.add_argument("--delay", type=float, default=0.5, help="Delay in seconds between API calls.")
    args = parser.parse_args()
    
    run_screening_experiment(
        task_limit=args.limit,
        model_override=args.model,
        runs_override=args.runs,
        delay_between_calls=args.delay
    )
