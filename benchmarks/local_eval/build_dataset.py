"""Build a frozen, control-verified non-Docker Python pytest benchmark set.

Reads single-file pure-Python tasks from a JSONL file or HuggingFace dataset
(e.g. princeton-nlp/SWE-bench_Verified or SWE-bench_Lite), sets up cached per-repo
virtualenvs without Docker, verifies each task passes the 3-part control check:
  1. Target FAIL_TO_PASS test fails on base_commit
  2. Existing PASS_TO_PASS regression subset passes on base_commit
  3. Applying the reference patch flips FAIL_TO_PASS to pass while preserving PASS_TO_PASS

Then writes deterministic, disjoint ``dev.jsonl`` and ``heldout.jsonl`` manifests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

PURE_PYTHON_REPOS = frozenset(
    {
        "psf/requests",
        "pallets/flask",
        "pallets/click",
        "pytest-dev/pytest",
        "sympy/sympy",
        "pylint-dev/pylint",
        "sphinx-doc/sphinx",
        "marshmallow-code/marshmallow",
        "python-attrs/attrs",
        "encode/httpx",
        "more-itertools/more-itertools",
        "pypa/packaging",
        "django/django",
    }
)


def _extract_modified_files(patch_text: str) -> List[str]:
    files: List[str] = []
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            rel = line[6:].strip()
            if rel != "/dev/null" and rel not in files:
                files.append(rel)
    return files


def _is_eligible_single_file_task(row: Dict[str, Any], allowed_repos: set[str]) -> Optional[str]:
    repo = str(row.get("repo", "")).strip()
    if allowed_repos and repo not in allowed_repos:
        return None
    patch_text = str(row.get("patch", "") or "")
    files = _extract_modified_files(patch_text)
    if len(files) != 1:
        return None
    target = files[0]
    if not target.endswith(".py") or target.startswith("tests/") or "/test_" in target:
        return None
    return target


def _parse_test_list(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        raw_s = raw.strip()
        if raw_s.startswith("["):
            try:
                parsed = json.loads(raw_s)
                if isinstance(parsed, list):
                    return [str(x) for x in parsed]
            except json.JSONDecodeError:
                pass
        return [x.strip() for x in raw_s.splitlines() if x.strip()]
    return []


def _ensure_repo_clone(repo: str, cache_dir: Path) -> Path:
    safe_name = repo.replace("/", "__")
    dest = cache_dir / "repos" / safe_name
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", f"https://github.com/{repo}.git", str(dest)],
            check=True,
        )
    return dest


def _ensure_repo_venv(repo: str, repo_dir: Path, cache_dir: Path) -> Path:
    safe_name = repo.replace("/", "__")
    venv_dir = cache_dir / "venvs" / safe_name
    py_exe = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not py_exe.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
        subprocess.run([str(py_exe), "-m", "pip", "install", "-q", "--upgrade", "pip", "pytest"], check=True)
        subprocess.run(
            [str(py_exe), "-m", "pip", "install", "-q", "--no-build-isolation", "-e", str(repo_dir)],
            check=False,
        )
    return py_exe


def _run_pytest_nodes(py_exe: Path, cwd: Path, nodes: List[str], timeout: int = 120) -> bool:
    if not nodes:
        return False
    proc = subprocess.run(
        [str(py_exe), "-m", "pytest", "-q", "-p", "no:cacheprovider", *nodes],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode == 0


def _verify_control(
    row: Dict[str, Any],
    target_file: str,
    cache_dir: Path,
    max_regression_tests: int = 15,
) -> Optional[Dict[str, Any]]:
    repo = str(row["repo"])
    base_commit = str(row["base_commit"])
    fail_to_pass = _parse_test_list(row.get("FAIL_TO_PASS"))
    pass_to_pass = _parse_test_list(row.get("PASS_TO_PASS"))[:max_regression_tests]
    if not fail_to_pass:
        return None

    repo_dir = _ensure_repo_clone(repo, cache_dir)
    with tempfile.TemporaryDirectory(prefix="argus_ctrl_") as tmp:
        work = Path(tmp) / "repo"
        subprocess.run(["git", "clone", "--local", "--no-hardlinks", str(repo_dir), str(work)], check=True)
        subprocess.run(["git", "checkout", "-q", base_commit], cwd=str(work), check=True)
        py_exe = _ensure_repo_venv(repo, work, cache_dir)

        # 1. Target must FAIL before patch
        if _run_pytest_nodes(py_exe, work, fail_to_pass):
            return None
        # 2. Regression subset must PASS before patch (if any)
        if pass_to_pass and not _run_pytest_nodes(py_exe, work, pass_to_pass):
            return None

        # 3. Reference patch + test_patch must flip FAIL_TO_PASS -> PASS
        for patch_field in ("test_patch", "patch"):
            p_text = str(row.get(patch_field) or "")
            if p_text.strip():
                proc = subprocess.run(
                    ["git", "apply", "--whitespace=nowarn", "-"],
                    input=p_text,
                    text=True,
                    cwd=str(work),
                    capture_output=True,
                )
                if proc.returncode != 0:
                    return None

        if not _run_pytest_nodes(py_exe, work, fail_to_pass):
            return None
        if pass_to_pass and not _run_pytest_nodes(py_exe, work, pass_to_pass):
            return None

    return {
        "task_id": str(row.get("instance_id") or hashlib.sha256(f"{repo}:{base_commit}".encode()).hexdigest()[:12]),
        "repo": repo,
        "base_commit": base_commit,
        "target_file": target_file,
        "problem_statement": str(row.get("problem_statement") or ""),
        "fail_to_pass": fail_to_pass,
        "pass_to_pass": pass_to_pass,
        "test_patch": str(row.get("test_patch") or ""),
    }


def _load_candidates(source: str) -> List[Dict[str, Any]]:
    src_path = Path(source)
    if src_path.is_file():
        return [json.loads(line) for line in src_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    from datasets import load_dataset  # optional: pip install datasets
    ds = load_dataset(source, split="test")
    return [dict(row) for row in ds]


def main() -> None:
    ap = argparse.ArgumentParser(description="Build frozen non-Docker Dev/Heldout pytest manifests.")
    ap.add_argument("--source", default="princeton-nlp/SWE-bench_Verified", help="Local JSONL path or HF dataset name")
    ap.add_argument("--out-dir", default="benchmarks/local_eval/manifests")
    ap.add_argument("--cache-dir", default=".argus_bench_cache")
    ap.add_argument("--dev-count", type=int, default=40)
    ap.add_argument("--heldout-count", type=int, default=80)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--repos", nargs="*", default=sorted(PURE_PYTHON_REPOS))
    ap.add_argument("--skip-control", action="store_true", help="Skip cloning/control pytest (dry-run filtering only)")
    args = ap.parse_args()

    allowed = set(args.repos)
    raw_rows = _load_candidates(args.source)
    candidates: List[tuple[Dict[str, Any], str]] = []
    for row in raw_rows:
        target = _is_eligible_single_file_task(row, allowed)
        if target:
            candidates.append((row, target))

    rng = random.Random(args.seed)
    rng.shuffle(candidates)

    needed = args.dev_count + args.heldout_count
    verified: List[Dict[str, Any]] = []
    cache_dir = Path(args.cache_dir).resolve()
    for row, target in candidates:
        if len(verified) >= needed:
            break
        if args.skip_control:
            verified.append(
                {
                    "task_id": str(row.get("instance_id", len(verified))),
                    "repo": str(row["repo"]),
                    "base_commit": str(row["base_commit"]),
                    "target_file": target,
                    "problem_statement": str(row.get("problem_statement") or ""),
                    "fail_to_pass": _parse_test_list(row.get("FAIL_TO_PASS")),
                    "pass_to_pass": _parse_test_list(row.get("PASS_TO_PASS"))[:15],
                    "test_patch": str(row.get("test_patch") or ""),
                }
            )
            continue
        item = _verify_control(row, target, cache_dir)
        if item:
            verified.append(item)
            print(f"[{len(verified)}/{needed}] verified {item['task_id']}", flush=True)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dev_items = verified[: args.dev_count]
    heldout_items = verified[args.dev_count : args.dev_count + args.heldout_count]

    (out_dir / "dev_40.jsonl").write_text(
        "\n".join(json.dumps(x) for x in dev_items) + ("\n" if dev_items else ""),
        encoding="utf-8",
    )
    (out_dir / "heldout_80.jsonl").write_text(
        "\n".join(json.dumps(x) for x in heldout_items) + ("\n" if heldout_items else ""),
        encoding="utf-8",
    )
    print(f"Wrote {len(dev_items)} dev tasks and {len(heldout_items)} heldout tasks to {out_dir}")


if __name__ == "__main__":
    main()
