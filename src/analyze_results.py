"""Empirical Research Analysis & Visualization Suite for Preliminary Study.

Answers the 3 core research questions with statistical rigor:
1. RQ1: Can context close the capability gap? (Pass rates, baseline vs marginal)
2. RQ2: Where does context fail? (Category breakdowns with exact counts k/n, Error taxonomy)
3. RQ3: Is the tradeoff worth it? (Model scale savings vs Context accuracy premium,
   Escalation cascade economics, Median/P95 latency analysis, Cost per 100k solved tasks)
"""

import os
import sys
import json
import pandas as pd
import numpy as np
from typing import Dict, Any


def run_full_analysis(
    baseline_csv: str = "results/baseline_screening.csv",
    context_csv: str = "results/context_experiment.csv",
    pilot_json: str = "data/pilot_tasks.json"
) -> Dict[str, Any]:
    """Performs end-to-end academic analysis on experimental logs."""
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

    # 2. Frontier (120b) Baseline on Pilot Set
    large_df = df_base[df_base["model"].str.contains("120b", case=False)]
    p_large = float(large_df["passed"].mean())
    c_large = float(large_df["estimated_cost_usd"].mean())
    l_large_mean = float(large_df["latency_ms"].mean())
    l_large_p50 = float(large_df["latency_ms"].median())

    # 3. Strategy Aggregations on Small Model
    strat_df = df_ctx.groupby("strategy").agg(
        total_runs=("passed", "count"),
        passed_runs=("passed", "sum"),
        pass_rate=("passed", "mean"),
        mean_input_tokens=("input_tokens", "mean"),
        mean_cost_usd=("estimated_cost_usd", "mean"),
        retrieval_p50=("retrieval_latency_ms", "median"),
        lat_mean=("total_latency_ms", "mean"),
        lat_p50=("total_latency_ms", "median"),
        lat_p95=("total_latency_ms", lambda x: x.quantile(0.95))
    ).reset_index()

    stats = strat_df.set_index("strategy").to_dict("index")
    p_s0 = stats.get("S0", {}).get("pass_rate", float(df_base[df_base["model"].str.contains("20b", case=False)]["passed"].mean()))
    c_s0 = stats.get("S0", {}).get("mean_cost_usd", 0.000064)
    cost_per_correct_s0 = c_s0 / p_s0 if p_s0 > 0 else 0.0

    # 4. Table 1: RQ1 Model Comparison & Pass Rates
    rq1_rows = []
    # Add Frontier Large Row
    rq1_rows.append({
        "Configuration": "Frontier Large (120b S0)",
        "Pass Rate": f"{p_large * 100:.1f}% ({int(large_df['passed'].sum())}/{len(large_df)})",
        "Cost / 100k Solved": f"${(c_large / p_large * 100_000):.2f}",
        "Latency Median (P50)": f"{l_large_p50:.0f} ms",
        "Latency Mean": f"{l_large_mean:.0f} ms"
    })

    label_map = {
        "S0": "Small (20b S0 Zero-Shot)",
        "S1": "Small (20b S1 Few-Shot)",
        "S2": "Small (20b S2 RAG Docs)",
        "S3": "Small (20b S3 Hybrid)"
    }

    for s in ["S0", "S1", "S2", "S3"]:
        if s not in stats:
            continue
        row = stats[s]
        p_s = row["pass_rate"]
        c_s = row["mean_cost_usd"]
        cost_per_correct = c_s / p_s if p_s > 0 else 0.0
        
        rq1_rows.append({
            "Configuration": label_map.get(s, s),
            "Pass Rate": f"{p_s * 100:.1f}% ({int(row['passed_runs'])}/{int(row['total_runs'])})",
            "Cost / 100k Solved": f"${(cost_per_correct * 100_000):.2f}",
            "Latency Median (P50)": f"{row['lat_p50']:.0f} ms",
            "Latency Mean": f"{row['lat_mean']:.0f} ms"
        })

    table_rq1 = pd.DataFrame(rq1_rows)

    # 5. Table 2: RQ2 Failure Boundary by Category (with Exact k/n Counts)
    df_ctx["category"] = df_ctx["task_id"].map(pilot_meta)
    
    cat_order = ["A_easy", "B_gap", "C_borderline", "D_hard"]
    cat_rows = []
    for cat in cat_order:
        cat_df = df_ctx[df_ctx["category"] == cat]
        row_data = {"Category": cat}
        for s in ["S0", "S1", "S2", "S3"]:
            s_cat = cat_df[cat_df["strategy"] == s]
            if len(s_cat) > 0:
                pass_k = int(s_cat["passed"].sum())
                total_n = len(s_cat)
                row_data[s] = f"{pass_k/total_n*100:.1f}% ({pass_k}/{total_n})"
            else:
                row_data[s] = "N/A"
        cat_rows.append(row_data)
    table_rq2_category = pd.DataFrame(cat_rows)

    err_pivot = df_ctx[df_ctx["error_type"].notna()].groupby(["strategy", "error_type"]).size().unstack(fill_value=0)

    # 6. Table 3: RQ3 Two-Dimensional Economics & Escalation Cascade
    rq3_rows = []
    direct_120b_cost_per_100k = (c_large / p_large) * 100_000

    for s in ["S0", "S1", "S2", "S3"]:
        if s not in stats:
            continue
        row = stats[s]
        p_s = row["pass_rate"]
        c_s = row["mean_cost_usd"]
        cost_per_correct = c_s / p_s if p_s > 0 else 0.0
        
        # Marginal context cost premium over S0
        marginal_premium_vs_s0 = ((cost_per_correct - cost_per_correct_s0) / cost_per_correct_s0 * 100.0) if cost_per_correct_s0 > 0 else 0.0
        
        # Savings vs Large 120b baseline
        savings_vs_120b = ((direct_120b_cost_per_100k - cost_per_correct * 100_000) / direct_120b_cost_per_100k * 100.0)
        
        # Production Escalation Cascade:
        # Expected cost = P(s)*C(s) + (1-P(s))*(C(s) + C(large))
        # Effective pass rate = P(s) + (1-P(s))*P(large)
        cascade_expected_cost = p_s * c_s + (1.0 - p_s) * (c_s + c_large)
        cascade_effective_pass = p_s + (1.0 - p_s) * p_large
        cascade_cost_per_100k_solved = (cascade_expected_cost / cascade_effective_pass) * 100_000
        cascade_savings_vs_120b = ((direct_120b_cost_per_100k - cascade_cost_per_100k_solved) / direct_120b_cost_per_100k * 100.0)

        rq3_rows.append({
            "Strategy": s,
            "Input Tokens": int(row["mean_input_tokens"]),
            "Standalone Cost / 100k Solved": f"${(cost_per_correct * 100_000):.2f}",
            "Marginal Cost vs S0": f"{marginal_premium_vs_s0:+.1f}%",
            "Standalone Savings vs 120B": f"{savings_vs_120b:.1f}%",
            "Cascade Cost / 100k Solved (Escalated)": f"${cascade_cost_per_100k_solved:.2f}",
            "Cascade Net Savings vs 120B": f"{cascade_savings_vs_120b:.1f}%",
            "Median Latency (P50)": f"{row['lat_p50']:.0f} ms"
        })

    table_rq3 = pd.DataFrame(rq3_rows)

    return {
        "table_rq1": table_rq1,
        "table_rq2_category": table_rq2_category,
        "table_rq2_errors": err_pivot,
        "table_rq3": table_rq3,
        "total_valid_runs": len(df_ctx),
        "direct_120b_cost_100k": direct_120b_cost_per_100k
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    res = run_full_analysis()
    print("================================================================================")
    print(f"       RIGOROUS PRELIMINARY STUDY REPORT (Evaluations: {res['total_valid_runs']})")
    print("================================================================================\n")
    print("--- TABLE 1 (RQ1): MODEL CAPABILITY & ACCURACY ---")
    print(res["table_rq1"].to_string(index=False))
    print("\n--- TABLE 2 (RQ2): FAILURE BOUNDARY BY CATEGORY (k/n counts) ---")
    print(res["table_rq2_category"].to_string(index=False))
    print("\n--- TABLE 2B: ERROR TAXONOMY BREAKDOWN ---")
    print(res["table_rq2_errors"].to_string())
    print("\n--- TABLE 3 (RQ3): TWO-DIMENSIONAL INFERENCE ECONOMICS & PRODUCTION CASCADE ---")
    print(f"Direct Frontier 120B Cost Baseline: ${res['direct_120b_cost_100k']:.2f} per 100,000 solved tasks\n")
    print(res["table_rq3"].to_string(index=False))
    print("================================================================================")
