"""Code Quality & Structural Complexity Benchmark.

Compares the actual generated code quality across:
1. Frontier Large Model (120b S0)
2. Small Model (20b S0 Zero-Shot)
3. Small Model + RAG (20b S2 RAG)

Evaluates:
- Cyclomatic Complexity (AST decision branching)
- Lines of Code (LOC) & AST Node Count
- Standard Library & Comprehension Idioms
- Execution Runtime
"""

import os
import sys
import json
import time
import pandas as pd
from typing import Dict, Any, List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.groq_client import GroqClient
from src.retrieval import ContextRetriever
from src.prompt_builder import build_prompt, SYSTEM_PROMPT
from src.evaluator import evaluate, sanitize_generated_code
from src.quality_analyzer import evaluate_quality


def run_quality_benchmark(
    pilot_json: str = "data/pilot_tasks.json",
    sample_size: int = 6,
    output_json: str = "results/code_quality_comparison.json"
) -> Dict[str, Any]:
    """Runs a quality comparison benchmark on a representative sample of pilot tasks."""
    with open(pilot_json, "r", encoding="utf-8") as f:
        pilot_tasks = json.load(f)

    # Pick balanced representative tasks (Category B Gap and Category A/C)
    cat_b = [t for t in pilot_tasks if t.get("category") == "B_gap"][:4]
    cat_a = [t for t in pilot_tasks if t.get("category") == "A_easy"][:2]
    selected_tasks = cat_b + cat_a

    groq = GroqClient()
    retriever = ContextRetriever()

    results = []

    print(f"[*] Running Code Quality Comparison on {len(selected_tasks)} representative tasks...")

    for t in selected_tasks:
        task_id = t["task_id"]
        prompt = t["prompt"]
        entry_point = t.get("entry_point")
        test_code = t["test_code"]
        category = t.get("category", "")

        print(f"\n--- Analyzing Task: {task_id} (Category: {category}) ---")

        # 1. Large Model (120b S0)
        p_large, _ = build_prompt("S0", t, retriever=retriever)
        res_large = groq.query(prompt=p_large, model="openai/gpt-oss-120b", system_prompt=SYSTEM_PROMPT, max_tokens=1024)
        code_large = sanitize_generated_code(res_large.get("content", ""))
        eval_large = evaluate(code_large, test_code)
        qual_large = evaluate_quality(code_large)

        # 2. Small Model (20b S0 Zero-Shot)
        p_small_s0, _ = build_prompt("S0", t, retriever=retriever)
        res_small_s0 = groq.query(prompt=p_small_s0, model="openai/gpt-oss-20b", system_prompt=SYSTEM_PROMPT, max_tokens=1024)
        code_small_s0 = sanitize_generated_code(res_small_s0.get("content", ""))
        eval_small_s0 = evaluate(code_small_s0, test_code)
        qual_small_s0 = evaluate_quality(code_small_s0)

        # 3. Small Model + RAG (20b S2)
        p_small_s2, _ = build_prompt("S2", t, retriever=retriever)
        res_small_s2 = groq.query(prompt=p_small_s2, model="openai/gpt-oss-20b", system_prompt=SYSTEM_PROMPT, max_tokens=1024)
        code_small_s2 = sanitize_generated_code(res_small_s2.get("content", ""))
        eval_small_s2 = evaluate(code_small_s2, test_code)
        qual_small_s2 = evaluate_quality(code_small_s2)

        record = {
            "task_id": task_id,
            "category": category,
            "prompt": prompt[:100] + "...",
            "large_120b": {
                "passed": eval_large["passed"],
                "exec_time_s": eval_large["execution_time_s"],
                "loc": qual_large["loc"],
                "ast_nodes": qual_large["ast_nodes"],
                "cyclomatic_complexity": qual_large["cyclomatic_complexity"],
                "comprehensions": qual_large["has_comprehensions"],
                "stdlib_imports": qual_large["stdlib_imports"],
                "code": code_large
            },
            "small_20b_s0": {
                "passed": eval_small_s0["passed"],
                "exec_time_s": eval_small_s0["execution_time_s"],
                "loc": qual_small_s0["loc"],
                "ast_nodes": qual_small_s0["ast_nodes"],
                "cyclomatic_complexity": qual_small_s0["cyclomatic_complexity"],
                "comprehensions": qual_small_s0["has_comprehensions"],
                "stdlib_imports": qual_small_s0["stdlib_imports"],
                "code": code_small_s0
            },
            "small_20b_s2_rag": {
                "passed": eval_small_s2["passed"],
                "exec_time_s": eval_small_s2["execution_time_s"],
                "loc": qual_small_s2["loc"],
                "ast_nodes": qual_small_s2["ast_nodes"],
                "cyclomatic_complexity": qual_small_s2["cyclomatic_complexity"],
                "comprehensions": qual_small_s2["has_comprehensions"],
                "stdlib_imports": qual_small_s2["stdlib_imports"],
                "code": code_small_s2
            }
        }
        results.append(record)
        time.sleep(1.0)

    # Save detailed code artifacts
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    # Summary table
    df_rows = []
    for r in results:
        df_rows.append({
            "Task": r["task_id"],
            "Cat": r["category"],
            "120B Pass": "PASS" if r["large_120b"]["passed"] else "FAIL",
            "120B CC": r["large_120b"]["cyclomatic_complexity"],
            "120B LOC": r["large_120b"]["loc"],
            "20B S0 Pass": "PASS" if r["small_20b_s0"]["passed"] else "FAIL",
            "20B S0 CC": r["small_20b_s0"]["cyclomatic_complexity"],
            "20B S0 LOC": r["small_20b_s0"]["loc"],
            "20B RAG Pass": "PASS" if r["small_20b_s2_rag"]["passed"] else "FAIL",
            "20B RAG CC": r["small_20b_s2_rag"]["cyclomatic_complexity"],
            "20B RAG LOC": r["small_20b_s2_rag"]["loc"],
        })

    summary_df = pd.DataFrame(df_rows)
    return {
        "summary_table": summary_df,
        "results": results
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    out = run_quality_benchmark()
    print("\n================================================================================")
    print("                 CODE QUALITY & STRUCTURAL COMPLEXITY RESULTS")
    print("================================================================================\n")
    print(out["summary_table"].to_string(index=False))
