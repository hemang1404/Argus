"""Empirical Metrics, Gap Closure & Inference Economics Calculator.

Computes academic metrics, Gap Closure %, and production cascade economics
directly from baseline_screening.csv and context_experiment.csv.
"""

import json
import os
import pandas as pd
import numpy as np
from typing import Dict, Any


def compute_comprehensive_metrics(
    baseline_csv: str = "results/baseline_screening.csv",
    context_csv: str = "results/context_experiment.csv",
    pilot_json: str = "data/pilot_tasks.json"
) -> Dict[str, Any]:
    """Computes all empirical metrics and economic models from experimental logs."""
    # 1. Load Data
    with open(pilot_json, "r", encoding="utf-8") as f:
        pilot_tasks = json.load(f)
    pilot_ids = set([t["task_id"] for t in pilot_tasks])
    pilot_meta = {t["task_id"]: t.get("category", "") for t in pilot_tasks}

    df_base = pd.read_csv(baseline_csv)
    df_base = df_base[df_base["task_id"].isin(pilot_ids)]
    df_base = df_base[df_base["error_type"] != "api_error"]

    df_ctx = pd.read_csv(context_csv)
    df_ctx = df_ctx[df_ctx["task_id"].isin(pilot_ids)]
    df_ctx = df_ctx[df_ctx["error_type"] != "api_error"]

    # 2. Compute Pass Rates on the 50 Pilot Tasks
    large_s0_df = df_base[df_base["model"].str.contains("120b", case=False)]
    p_large_s0 = float(large_s0_df["passed"].mean())

    ctx_stats = df_ctx.groupby("strategy").agg(
        pass_rate=("passed", "mean"),
        mean_input_tokens=("input_tokens", "mean"),
        mean_output_tokens=("output_tokens", "mean"),
        mean_cost_usd=("estimated_cost_usd", "mean"),
        mean_retrieval_ms=("retrieval_latency_ms", "mean"),
        mean_llm_ms=("llm_latency_ms", "mean"),
        mean_total_ms=("total_latency_ms", "mean")
    ).reset_index()

    stats_dict = ctx_stats.set_index("strategy").to_dict("index")
    
    p_small_s0 = stats_dict.get("S0", {}).get("pass_rate", float(df_base[df_base["model"].str.contains("20b", case=False)]["passed"].mean()))
    gap = p_large_s0 - p_small_s0

    # Measured Large Model Costs & Latency
    c_large = float(large_s0_df["estimated_cost_usd"].mean())
    l_large = float(large_s0_df["latency_ms"].mean())

    # 3. Gap Closure & Efficiency Analysis
    results_summary = []
    
    for strat in ["S0", "S1", "S2", "S3"]:
        if strat not in stats_dict:
            continue
            
        row = stats_dict[strat]
        p_strat = row["pass_rate"]
        c_strat = row["mean_cost_usd"]
        l_strat = row["mean_total_ms"]
        
        improvement = p_strat - p_small_s0
        gap_closure = (improvement / gap * 100.0) if gap > 0 else 0.0

        # Production Escalation Cascade Math:
        # If small passes (prob p_strat) -> cost is c_strat
        # If small fails (prob 1 - p_strat) -> escalates to large -> cost is c_strat + c_large
        expected_cascade_cost = p_strat * c_strat + (1.0 - p_strat) * (c_strat + c_large)
        expected_cascade_latency = p_strat * l_strat + (1.0 - p_strat) * (l_strat + l_large)
        
        # Effective pass rate of cascade = p_strat + (1 - p_strat) * p_large_s0
        cascade_effective_pass_rate = p_strat + (1.0 - p_strat) * p_large_s0
        effective_cost_per_pass = expected_cascade_cost / cascade_effective_pass_rate if cascade_effective_pass_rate > 0 else 0.0
        
        # Net savings vs 100% Direct Frontier Routing
        direct_frontier_effective_cost = c_large / p_large_s0 if p_large_s0 > 0 else 0.0
        cost_savings_pct = ((direct_frontier_effective_cost - effective_cost_per_pass) / direct_frontier_effective_cost * 100.0) if direct_frontier_effective_cost > 0 else 0.0

        results_summary.append({
            "Strategy": strat,
            "Pass Rate": f"{p_strat * 100:.1f}%",
            "Improvement (Delta_P)": f"{improvement * 100:+.1f}%",
            "Gap Closure %": f"{gap_closure:.1f}%",
            "Mean Tokens": int(row["mean_input_tokens"]),
            "Mean Latency (ms)": f"{l_strat:.1f}",
            "Cascade Expected Cost ($)": f"${expected_cascade_cost:.6f}",
            "Cascade Latency (ms)": f"{expected_cascade_latency:.1f}",
            "Effective Cost / Pass ($)": f"${effective_cost_per_pass:.6f}",
            "Cost Savings vs 120B (%)": f"{cost_savings_pct:.1f}%"
        })

    summary_df = pd.DataFrame(results_summary)

    # 4. Category Breakdown Analysis (A_easy, B_gap, C_borderline, D_hard)
    df_ctx["category"] = df_ctx["task_id"].map(pilot_meta)
    cat_summary = df_ctx.groupby(["category", "strategy"])["passed"].mean().unstack().fillna(0.0)
    cat_summary = (cat_summary * 100).round(1)

    return {
        "frontier_pass_rate": p_large_s0,
        "frontier_mean_cost": c_large,
        "frontier_mean_latency": l_large,
        "small_s0_pass_rate": p_small_s0,
        "capability_gap": gap,
        "summary_table": summary_df,
        "category_table": cat_summary
    }


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    if os.path.exists("results/context_experiment.csv") and os.path.getsize("results/context_experiment.csv") > 100:
        res = compute_comprehensive_metrics()
        print("=== FRONTIER VS SMALL MODEL BASELINE ===")
        print(f"Large Model (120b) S0 Baseline Pass Rate : {res['frontier_pass_rate']*100:.1f}% (Cost: ${res['frontier_mean_cost']:.6f}, Latency: {res['frontier_mean_latency']:.1f} ms)")
        print(f"Small Model (20b)  S0 Baseline Pass Rate : {res['small_s0_pass_rate']*100:.1f}%")
        print(f"Capability Gap to Close                   : {res['capability_gap']*100:.1f}%\n")
        
        print("=== EXPERIMENTAL STRATEGY COMPARISON & CASCADE ECONOMICS ===")
        print(res["summary_table"].to_string(index=False))
        
        print("\n=== ACCURACY BY TASK CATEGORY (%) ===")
        print(res["category_table"].to_string())
    else:
        print("[*] context_experiment.csv still collecting results in background.")
