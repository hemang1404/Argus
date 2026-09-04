"""Task Loader & Normalizer for Coding Benchmarks (MBPP & HumanEval).

This module downloads benchmark datasets from Hugging Face and normalizes them
into a unified schema saved as JSON Lines (JSONL).
"""

import json
import os
import re
from typing import Dict, List, Any
from datasets import load_dataset


def extract_mbpp_entry_point(code_str: str) -> str:
    """Extracts function name from canonical solution in MBPP."""
    match = re.search(r"def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", code_str)
    return match.group(1) if match else "solution"


def load_mbpp_tasks(target_count: int = 150) -> List[Dict[str, Any]]:
    """Loads and normalizes curated MBPP sanitized tasks from Hugging Face.
    
    MBPP sanitized structure:
      - prompt: Curated problem description
      - test_list: Validated assertions
      - test_imports: Required library imports
      - code: Canonical reference solution
    """
    print(f"[*] Fetching MBPP sanitized tasks (target: {target_count})...")
    # Using 'sanitized' split (test + train)
    splits = ["train", "test"]
    normalized_tasks = []
    
    for split_name in splits:
        if len(normalized_tasks) >= target_count:
            break
        raw_dataset = load_dataset("google-research-datasets/mbpp", "sanitized", split=split_name)
        for item in raw_dataset:
            if len(normalized_tasks) >= target_count:
                break
                
            task_id = f"mbpp_{item['task_id']}"
            prompt = item["prompt"].strip()
            test_list = item.get("test_list", [])
            test_imports = item.get("test_imports", [])
            
            # Combine imports + assertions
            combined_tests = []
            if test_imports:
                combined_tests.extend(test_imports)
            combined_tests.extend(test_list)
            test_code = "\n".join(combined_tests).strip()
            
            canonical_solution = item.get("code", "").strip()
            entry_point = extract_mbpp_entry_point(canonical_solution)
            
            normalized_tasks.append({
                "task_id": task_id,
                "source": "MBPP",
                "prompt": prompt,
                "entry_point": entry_point,
                "test_code": test_code,
                "canonical_solution": canonical_solution,
                "category": None
            })
        
    print(f"[+] Loaded {len(normalized_tasks)} MBPP sanitized tasks.")
    return normalized_tasks


def load_humaneval_tasks(target_count: int = 50) -> List[Dict[str, Any]]:
    """Loads and normalizes HumanEval tasks from Hugging Face.
    
    HumanEval structure:
      - prompt: Function signature + docstring
      - test: Unit test harness with check(candidate)
      - canonical_solution: Function body
      - entry_point: Function name
    """
    print(f"[*] Fetching HumanEval tasks (target: {target_count})...")
    raw_dataset = load_dataset("openai_humaneval", split="test")
    
    normalized_tasks = []
    for item in raw_dataset:
        if len(normalized_tasks) >= target_count:
            break
            
        clean_id = item["task_id"].replace("/", "_").lower()
        prompt = item["prompt"].strip()
        entry_point = item["entry_point"].strip()
        
        # Test harness in HumanEval defines check(candidate). We append check(entry_point) to execute it.
        raw_test = item["test"].strip()
        test_code = f"{raw_test}\ncheck({entry_point})"
        canonical_solution = prompt + "\n" + item["canonical_solution"]
        
        normalized_tasks.append({
            "task_id": clean_id,
            "source": "HumanEval",
            "prompt": prompt,
            "entry_point": entry_point,
            "test_code": test_code,
            "canonical_solution": canonical_solution,
            "category": None
        })
        
    print(f"[+] Loaded {len(normalized_tasks)} HumanEval tasks.")
    return normalized_tasks


def build_candidate_pool(
    output_path: str = "data/candidate_tasks.jsonl",
    mbpp_count: int = 150,
    humaneval_count: int = 50
) -> List[Dict[str, Any]]:
    """Builds the unified candidate pool (200 tasks total) and writes to JSONL."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    tasks = []
    tasks.extend(load_mbpp_tasks(mbpp_count))
    tasks.extend(load_humaneval_tasks(humaneval_count))
    
    with open(output_path, "w", encoding="utf-8") as f:
        for task in tasks:
            f.write(json.dumps(task, ensure_ascii=False) + "\n")
            
    print(f"[SUCCESS] Successfully wrote {len(tasks)} candidate tasks to {output_path}")
    return tasks


def load_candidate_tasks(file_path: str = "data/candidate_tasks.jsonl") -> List[Dict[str, Any]]:
    """Reads normalized candidate tasks from a JSONL file."""
    tasks = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                tasks.append(json.loads(line.strip()))
    return tasks


if __name__ == "__main__":
    build_candidate_pool()
