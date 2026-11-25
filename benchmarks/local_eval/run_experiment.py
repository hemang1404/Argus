"""Resumable per-task JSONL experiment runner and paired statistical summary tool.

Supports Experiment 1 (baseline vs. loop_kill vs. loop_kill_retry) against any
local OpenAI-compatible server (llama-server, vLLM, Ollama) or cloud endpoint.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

from argus.config import ArgusConfig
from argus.providers.models import ProviderConfig
from argus.runtime.controller import RecoveryController


def load_completed_ids(out_path: Path) -> set[str]:
    """Return task IDs already recorded in an existing JSONL output file."""
    if not out_path.is_file():
        return set()
    done: set[str] = set()
    for line in out_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            tid = obj.get("task_id")
            if tid:
                done.add(str(tid))
        except json.JSONDecodeError:
            continue
    return done


def exact_mcnemar_pvalue(b: int, c: int) -> float:
    """Two-sided exact McNemar binomial p-value for discordant pairs (b, c)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, j) for j in range(k + 1)) / (2 ** n)
    return min(1.0, 2.0 * tail)


def paired_bootstrap_ci(
    a_vals: List[int],
    b_vals: List[int],
    n_resamples: int = 5000,
    seed: int = 42,
) -> Tuple[float, float, float]:
    """Return (point_diff_pct, ci_low_pct, ci_high_pct) for A minus B."""
    n = len(a_vals)
    if n == 0:
        return 0.0, 0.0, 0.0
    diffs = [a - b for a, b in zip(a_vals, b_vals)]
    point = 100.0 * sum(diffs) / n
    rng = random.Random(seed)
    boots: List[float] = []
    for _ in range(n_resamples):
        s = sum(diffs[rng.randrange(n)] for _ in range(n))
        boots.append(100.0 * s / n)
    boots.sort()
    lo = boots[int(0.025 * n_resamples)]
    hi = boots[min(n_resamples - 1, int(0.975 * n_resamples))]
    return round(point, 2), round(lo, 2), round(hi, 2)


def summarize_paired(base_path: Path, treat_path: Path) -> Dict[str, Any]:
    """Compare two resumable JSONL result files paired by task_id."""
    base_rows = {
        r["task_id"]: r
        for line in base_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for r in [json.loads(line)]
    }
    treat_rows = {
        r["task_id"]: r
        for line in treat_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for r in [json.loads(line)]
    }
    common = sorted(set(base_rows) & set(treat_rows))
    a_solved = [1 if treat_rows[k].get("true_solved") else 0 for k in common]
    b_solved = [1 if base_rows[k].get("true_solved") else 0 for k in common]
    only_treat = sum(1 for a, b in zip(a_solved, b_solved) if a == 1 and b == 0)
    only_base = sum(1 for a, b in zip(a_solved, b_solved) if a == 0 and b == 1)

    diff_pct, lo_pct, hi_pct = paired_bootstrap_ci(a_solved, b_solved)
    p_val = exact_mcnemar_pvalue(only_treat, only_base)

    base_tokens = sum(int(base_rows[k].get("total_tokens", 0)) for k in common)
    treat_tokens = sum(int(treat_rows[k].get("total_tokens", 0)) for k in common)
    base_secs = sum(float(base_rows[k].get("duration_s", 0.0)) for k in common)
    treat_secs = sum(float(treat_rows[k].get("duration_s", 0.0)) for k in common)
    treat_aborts = sum(int(treat_rows[k].get("loop_aborts", 0)) for k in common)

    return {
        "n_paired": len(common),
        "baseline_solved": sum(b_solved),
        "treatment_solved": sum(a_solved),
        "diff_pct_points": diff_pct,
        "bootstrap_95_ci": [lo_pct, hi_pct],
        "mcnemar_p": round(p_val, 5),
        "only_treatment": only_treat,
        "only_baseline": only_base,
        "baseline_tokens": base_tokens,
        "treatment_tokens": treat_tokens,
        "baseline_duration_s": round(base_secs, 2),
        "treatment_duration_s": round(treat_secs, 2),
        "treatment_loop_aborts": treat_aborts,
    }


def _arm_config(arm: str, model: str, base_url: str, max_tokens: int, timeout_s: float) -> ArgusConfig:
    p_cfg = ProviderConfig(
        name="custom",
        base_url=base_url,
        model=model,
        temperature=0.0,
        max_tokens=max_tokens,
        timeout_seconds=timeout_s,
    )
    return ArgusConfig(
        provider="custom",
        model=model,
        provider_config=p_cfg,
        max_attempts=3,
        max_tokens=max_tokens,
        provider_timeout_seconds=timeout_s,
        budget_limit_usd=1.0,
        loop_guard_enabled=(arm in ("loop_kill", "loop_kill_retry")),
        trap_aware_retry=(arm == "loop_kill_retry"),
    )


def _run_single_task(
    task: Dict[str, Any],
    arm: str,
    model: str,
    base_url: str,
    cache_dir: Path,
    max_tokens: int,
    timeout_s: float,
) -> Dict[str, Any]:
    repo = task["repo"]
    safe_repo = repo.replace("/", "__")
    repo_src = cache_dir / "repos" / safe_repo
    venv_dir = cache_dir / "venvs" / safe_repo
    py_exe = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not py_exe.exists():
        py_exe = Path(sys.executable)

    with tempfile.TemporaryDirectory(prefix="argus_exp_") as tmp:
        work = Path(tmp) / "repo"
        subprocess.run(["git", "clone", "--local", "--no-hardlinks", str(repo_src), str(work)], check=True)
        subprocess.run(["git", "checkout", "-q", task["base_commit"]], cwd=str(work), check=True)
        test_patch = str(task.get("test_patch") or "")
        if test_patch.strip():
            subprocess.run(
                ["git", "apply", "--whitespace=nowarn", "-"],
                input=test_patch,
                text=True,
                cwd=str(work),
                check=True,
            )

        target_nodes = task["fail_to_pass"]
        reg_nodes = task.get("pass_to_pass") or []
        target_cmd = " ".join(shlex.quote(x) for x in [str(py_exe), "-m", "pytest", "-q", "-p", "no:cacheprovider", *target_nodes])

        cfg = _arm_config(arm, model, base_url, max_tokens, timeout_s)
        outcome = RecoveryController(config=cfg).run(
            target_file=task["target_file"],
            test_command=target_cmd,
            workspace_root=str(work),
            problem_statement=task.get("problem_statement"),
        )

        true_solved = False
        regressed = False
        if outcome.status == "SUCCESS" and outcome.patched_content:
            (work / task["target_file"]).write_text(outcome.patched_content, encoding="utf-8")
            if reg_nodes:
                reg_proc = subprocess.run(
                    [str(py_exe), "-m", "pytest", "-q", "-p", "no:cacheprovider", *reg_nodes],
                    cwd=str(work),
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                regressed = reg_proc.returncode != 0
                true_solved = not regressed
            else:
                true_solved = True

        total_tokens = sum(r.prompt_tokens + r.completion_tokens for r in outcome.history)
        loop_aborts = sum(1 for r in outcome.history if (r.finish_reason or "").lower() == "loop_aborted")
        tiers = [r.tier_used for r in outcome.history]
        return {
            "task_id": task["task_id"],
            "arm": arm,
            "model": model,
            "status": outcome.status,
            "target_passed": outcome.status == "SUCCESS",
            "regressed": regressed,
            "true_solved": true_solved,
            "attempts": outcome.total_attempts,
            "total_tokens": total_tokens,
            "loop_aborts": loop_aborts,
            "tiers": tiers,
            "duration_s": round(outcome.total_duration_ms / 1000.0, 2),
        }


def main() -> None:
    ap = argparse.ArgumentParser(description="Resumable Argus local-model experiment runner.")
    ap.add_argument("--manifest", help="Path to dev_40.jsonl or heldout_80.jsonl")
    ap.add_argument("--arm", choices=["baseline", "loop_kill", "loop_kill_retry"], default="baseline")
    ap.add_argument("--model", default="qwen2.5-coder-7b-instruct")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    ap.add_argument("--cache-dir", default=".argus_bench_cache")
    ap.add_argument("--out", default="benchmarks/local_eval/results/run.jsonl")
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--summarize", nargs=2, metavar=("BASELINE_JSONL", "TREATMENT_JSONL"))
    args = ap.parse_args()

    if args.summarize:
        summary = summarize_paired(Path(args.summarize[0]), Path(args.summarize[1]))
        print(json.dumps(summary, indent=2))
        return

    if not args.manifest:
        ap.error("--manifest is required when not using --summarize")

    manifest_path = Path(args.manifest)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    completed = load_completed_ids(out_path)
    tasks = [json.loads(ln) for ln in manifest_path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    cache_dir = Path(args.cache_dir).resolve()
    with out_path.open("a", encoding="utf-8") as fh:
        for idx, task in enumerate(tasks, 1):
            tid = task["task_id"]
            if tid in completed:
                continue
            row = _run_single_task(
                task=task,
                arm=args.arm,
                model=args.model,
                base_url=args.base_url,
                cache_dir=cache_dir,
                max_tokens=args.max_tokens,
                timeout_s=args.timeout,
            )
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            print(f"[{idx}/{len(tasks)}] {tid} -> {row['status']} (true_solved={row['true_solved']}, aborts={row['loop_aborts']})", flush=True)


if __name__ == "__main__":
    main()
