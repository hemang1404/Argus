"""Phase B/C Context Experiment Runner.

Executes the 50 frozen pilot tasks across 4 prompt strategies (S0, S1, S2, S3)
on the small model, tracking retrieval overhead, token inflation, latency, and pass rates.
"""

import os
import sys
import csv
import json
import time
import argparse
import yaml

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tqdm import tqdm
from typing import Set, Tuple, List, Dict, Any

from src.groq_client import GroqClient
from src.evaluator import evaluate
from src.retrieval import ContextRetriever
from src.prompt_builder import build_prompt, SYSTEM_PROMPT


def load_completed_context_runs(csv_path: str) -> Set[Tuple[str, str, str, int]]:
    """Loads set of (task_id, model, strategy, run_number) tuples that are already finished."""
    completed = set()
    if not os.path.exists(csv_path):
        return completed
        
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("error_type") != "api_error":
                    completed.add((row["task_id"], row["model"], row["strategy"], int(row["run_number"])))
    except Exception as e:
        print(f"[WARN] Error reading existing CSV ({e}), starting fresh check.")
    return completed


def run_context_experiment(
    config_path: str = "configs/experiment.yaml",
    pilot_tasks_path: str = "data/pilot_tasks.json",
    output_csv: str = "results/context_experiment.csv",
    task_limit: int = 0,
    runs_override: int = 0,
    delay_between_calls: float = 0.4
) -> None:
    """Runs the Phase B/C context injection experiment."""
    # 1. Load Experiment Config
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
        
    small_model = config["models"]["small"]
    runs_per_config = runs_override if runs_override > 0 else config["generation"].get("runs_per_config", 2)
    temperature = config["generation"].get("temperature", 0.2)
    max_tokens = config["generation"].get("max_tokens", 512)
    timeout_sec = config["evaluation"].get("timeout_seconds", 5)
    few_shot_k = config["context"].get("few_shot_k", 2)
    rag_k = config["context"].get("rag_chunks_k", 3)

    # 2. Load Frozen Pilot Tasks
    with open(pilot_tasks_path, "r", encoding="utf-8") as f:
        pilot_tasks = json.load(f)
        
    if task_limit > 0:
        pilot_tasks = pilot_tasks[:task_limit]
        print(f"[*] Limited to first {task_limit} pilot tasks for testing.")
        
    strategies = ["S0", "S1", "S2", "S3"]
    total_expected = len(pilot_tasks) * len(strategies) * runs_per_config
    
    print(f"[*] Loaded {len(pilot_tasks)} frozen pilot tasks.")
    print(f"[*] Target Model: {small_model}")
    print(f"[*] Strategies to evaluate: {strategies}")
    print(f"[*] Runs per configuration: {runs_per_config}")
    print(f"[*] Total API calls scheduled: {total_expected}")

    # 3. Setup CSV & Resume Tracking
    os.makedirs(os.path.dirname(output_csv), exist_ok=True)
    completed_runs = load_completed_context_runs(output_csv)
    print(f"[*] Found {len(completed_runs)} already completed runs.")

    fieldnames = [
        "task_id", "source", "category", "model", "strategy", "run_number",
        "passed", "error_type", "input_tokens", "output_tokens",
        "retrieval_latency_ms", "llm_latency_ms", "total_latency_ms",
        "estimated_cost_usd", "execution_time_s",
        "few_shot_ids", "rag_doc_ids", "error_msg"
    ]
    
    file_exists = os.path.exists(output_csv)
    csv_file = open(output_csv, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
    
    if not file_exists or os.path.getsize(output_csv) == 0:
        writer.writeheader()
        csv_file.flush()

    # 4. Initialize Groq Client & Semantic Retriever
    client = GroqClient()
    retriever = ContextRetriever()
    
    # 5. Execution Loop
    progress_bar = tqdm(total=total_expected, initial=len(completed_runs), desc="Context Experiment")
    
    try:
        for strategy in strategies:
            for run_num in range(1, runs_per_config + 1):
                for task in pilot_tasks:
                    task_id = task["task_id"]
                    
                    # Skip if already finished (Idempotency)
                    if (task_id, small_model, strategy, run_num) in completed_runs:
                        continue
                        
                    # Build Strategy Prompt
                    user_prompt, meta = build_prompt(
                        strategy=strategy,
                        task=task,
                        retriever=retriever,
                        few_shot_k=few_shot_k,
                        rag_k=rag_k
                    )
                    
                    retrieval_latency = meta.get("retrieval_latency_ms", 0.0)
                    
                    # Query Groq Small Model
                    query_res = client.query(
                        prompt=user_prompt,
                        model=small_model,
                        system_prompt=SYSTEM_PROMPT,
                        temperature=temperature,
                        max_tokens=max_tokens
                    )
                    
                    llm_latency = query_res["latency_ms"]
                    total_latency = round(retrieval_latency + llm_latency, 2)
                    
                    if not query_res["success"]:
                        row = {
                            "task_id": task_id,
                            "source": task["source"],
                            "category": task.get("category", ""),
                            "model": small_model,
                            "strategy": strategy,
                            "run_number": run_num,
                            "passed": False,
                            "error_type": "api_error",
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "retrieval_latency_ms": retrieval_latency,
                            "llm_latency_ms": llm_latency,
                            "total_latency_ms": total_latency,
                            "estimated_cost_usd": 0.0,
                            "execution_time_s": 0.0,
                            "few_shot_ids": json.dumps(meta.get("few_shot_ids", [])),
                            "rag_doc_ids": json.dumps(meta.get("rag_doc_ids", [])),
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
                            "category": task.get("category", ""),
                            "model": small_model,
                            "strategy": strategy,
                            "run_number": run_num,
                            "passed": eval_res["passed"],
                            "error_type": eval_res["error_type"] or "",
                            "input_tokens": query_res["input_tokens"],
                            "output_tokens": query_res["output_tokens"],
                            "retrieval_latency_ms": retrieval_latency,
                            "llm_latency_ms": llm_latency,
                            "total_latency_ms": total_latency,
                            "estimated_cost_usd": query_res["estimated_cost_usd"],
                            "execution_time_s": eval_res["execution_time_s"],
                            "few_shot_ids": json.dumps(meta.get("few_shot_ids", [])),
                            "rag_doc_ids": json.dumps(meta.get("rag_doc_ids", [])),
                            "error_msg": eval_res["error_msg"][:200]
                        }

                    # Write immediately to CSV (crash-safe)
                    writer.writerow(row)
                    csv_file.flush()
                    
                    completed_runs.add((task_id, small_model, strategy, run_num))
                    progress_bar.update(1)
                    
                    if delay_between_calls > 0:
                        time.sleep(delay_between_calls)

    finally:
        csv_file.close()
        progress_bar.close()
        print(f"\n[SUCCESS] Context experiment complete. Results saved to {output_csv}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Phase B/C context experiment on pilot tasks.")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of pilot tasks to test.")
    parser.add_argument("--runs", type=int, default=2, help="Number of runs per task (default: 2).")
    parser.add_argument("--delay", type=float, default=0.4, help="Delay in seconds between API calls.")
    args = parser.parse_args()
    
    run_context_experiment(
        task_limit=args.limit,
        runs_override=args.runs,
        delay_between_calls=args.delay
    )
