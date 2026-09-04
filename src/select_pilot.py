"""Task Categorization & Pilot Set Selection.

Analyzes Phase A baseline results, classifies candidate tasks into Quadrants
(A_easy, B_gap, C_borderline, D_hard), and selects a balanced 50-task pilot benchmark.
"""

import json
import os
import pandas as pd
from typing import Dict, List, Any


def classify_task(row: pd.Series) -> str:
    """Classifies a task based on Small vs Large model pass rates."""
    s = row["small_pass_rate"]
    l = row["large_pass_rate"]
    
    # Category B: Model Gap (Large succeeds, Small struggles)
    if s <= 0.50 and l >= 0.50:
        return "B_gap"
    # Category A: Easy (Both models reliably pass)
    elif s >= 0.67 and l >= 0.67:
        return "A_easy"
    # Category D: Hard (Both models struggle)
    elif s <= 0.33 and l <= 0.33:
        return "D_hard"
    # Category C: Borderline / Inconsistent
    else:
        return "C_borderline"


def build_pilot_dataset(
    csv_path: str = "results/baseline_screening.csv",
    tasks_path: str = "data/candidate_tasks.jsonl",
    output_path: str = "data/pilot_tasks.json"
) -> Dict[str, Any]:
    # 1. Load CSV and filter valid runs
    df = pd.read_csv(csv_path)
    valid_df = df[df["error_type"] != "api_error"]

    # 2. Compute per-task pass rate for each model
    task_stats = valid_df.groupby(["task_id", "model"])["passed"].mean().unstack()
    task_stats.columns = ["large_pass_rate", "small_pass_rate"]
    task_stats = task_stats.fillna(0.0)

    # 3. Classify all tasks
    task_stats["category"] = task_stats.apply(classify_task, axis=1)

    print("=== Candidate Pool Task Breakdown ===")
    print(task_stats["category"].value_counts())

    # 4. Load full task objects from candidate JSONL
    tasks_by_id = {}
    with open(tasks_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                item = json.loads(line.strip())
                tasks_by_id[item["task_id"]] = item

    # 5. Sample balanced 50-task pilot set:
    # Target: 20 B_gap, 15 C_borderline, 10 A_easy, 5 D_hard
    categories = {
        "B_gap": task_stats[task_stats["category"] == "B_gap"].index.tolist(),
        "C_borderline": task_stats[task_stats["category"] == "C_borderline"].index.tolist(),
        "A_easy": task_stats[task_stats["category"] == "A_easy"].index.tolist(),
        "D_hard": task_stats[task_stats["category"] == "D_hard"].index.tolist(),
    }

    selected_ids = []
    
    # Pick target quotas (or all available if pool is smaller)
    b_pick = categories["B_gap"][:20]
    c_pick = categories["C_borderline"][:15]
    a_pick = categories["A_easy"][:10]
    d_pick = categories["D_hard"][:5]

    selected_ids = b_pick + c_pick + a_pick + d_pick
    
    # Fill to 50 if needed from remaining borderline or easy
    if len(selected_ids) < 50:
        remaining = [t for t in task_stats.index if t not in selected_ids]
        selected_ids.extend(remaining[:50 - len(selected_ids)])

    # 6. Assemble Pilot Tasks
    pilot_tasks = []
    for tid in selected_ids[:50]:
        task_data = tasks_by_id[tid].copy()
        task_data["category"] = task_stats.loc[tid, "category"]
        task_data["small_baseline_pass_rate"] = float(task_stats.loc[tid, "small_pass_rate"])
        task_data["large_baseline_pass_rate"] = float(task_stats.loc[tid, "large_pass_rate"])
        pilot_tasks.append(task_data)

    # 7. Write to data/pilot_tasks.json and FREEZE it
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(pilot_tasks, f, indent=2, ensure_ascii=False)

    print(f"\n[SUCCESS] Frozen {len(pilot_tasks)} pilot tasks to {output_path}")
    pilot_df = pd.DataFrame(pilot_tasks)
    print("\n=== Final Frozen Pilot Composition ===")
    print(pilot_df["category"].value_counts())
    print("\nSource breakdown:")
    print(pilot_df["source"].value_counts())
    
    return {"total": len(pilot_tasks), "categories": pilot_df["category"].value_counts().to_dict()}


if __name__ == "__main__":
    build_pilot_dataset()
