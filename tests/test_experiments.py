"""Unit tests for Phase 4 experiment runners."""

import os
import sys
import tempfile
import json
import csv

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.experiments.run_isocost import run_isocost_experiment
from src.experiments.run_marginal_returns import run_marginal_returns_experiment
from src.experiments.run_intervention_matrix import run_intervention_matrix_experiment


def create_temp_task_file():
    tasks = [
        {
            "task_id": "test_t1",
            "source": "HumanEval",
            "prompt": "def add(a, b):",
            "entry_point": "add",
            "test_code": "assert add(2, 3) == 5\nassert add(1, 1) == 2"
        },
        {
            "task_id": "test_t2",
            "source": "MBPP",
            "prompt": "def mult(a, b):",
            "entry_point": "mult",
            "test_code": "assert mult(3, 4) == 12"
        }
    ]
    f = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8")
    json.dump(tasks, f)
    f.close()
    return f.name


def test_isocost_smoke():
    print("=== Testing Iso-Cost Experiment Runner (Smoke) ===")
    task_file = create_temp_task_file()
    out_dir = tempfile.mkdtemp()

    try:
        res = run_isocost_experiment(
            tasks_path=task_file,
            num_tasks=2,
            cost_budget_usd=0.003,
            max_attempts=2,
            output_dir=out_dir,
            selected_strategies=["always_repair", "single_shot_large"]
        )
        assert os.path.exists(res["attempts_csv"])
        assert os.path.exists(res["summary_csv"])
        assert len(res["summary"]) == 2
        print("Iso-Cost Smoke Test: OK")
    finally:
        if os.path.exists(task_file):
            os.remove(task_file)


def test_marginal_returns_smoke():
    print("=== Testing Marginal Returns Experiment Runner (Smoke) ===")
    task_file = create_temp_task_file()
    out_dir = tempfile.mkdtemp()

    try:
        res = run_marginal_returns_experiment(
            tasks_path=task_file,
            num_tasks=2,
            max_attempts=3,
            cost_budget_usd=0.01,
            output_dir=out_dir
        )
        assert os.path.exists(res["attempts_csv"])
        assert os.path.exists(res["curve_csv"])
        assert len(res["curve"]) > 0
        print("Marginal Returns Smoke Test: OK")
    finally:
        if os.path.exists(task_file):
            os.remove(task_file)


def test_intervention_matrix_smoke():
    print("=== Testing Intervention Matrix Experiment Runner (Smoke) ===")
    task_file = create_temp_task_file()
    out_dir = tempfile.mkdtemp()

    try:
        res = run_intervention_matrix_experiment(
            tasks_path=task_file,
            num_tasks=2,
            output_dir=out_dir
        )
        assert os.path.exists(res["matrix_csv"])
        assert os.path.exists(res["summary_csv"])
        print("Intervention Matrix Smoke Test: OK")
    finally:
        if os.path.exists(task_file):
            os.remove(task_file)


if __name__ == "__main__":
    test_isocost_smoke()
    test_marginal_returns_smoke()
    test_intervention_matrix_smoke()
    print("\n[SUCCESS] All Phase 4 experiment runners verified!")
