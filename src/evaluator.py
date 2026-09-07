"""Evaluator Module for AI-Generated Code Execution.

Executes code against test suites in an isolated OS subprocess with strict timeouts,
per-test assertion granularity, and detailed error taxonomy classification.
"""

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Dict, Any, Optional, List, Tuple


def sanitize_generated_code(code_str: str) -> str:
    """Strips markdown code blocks, backticks, and extraneous wrapper text.
    
    Examples handled:
      ```python
      def foo(): pass
      ```
      -> def foo(): pass
    """
    if not code_str:
        return ""
        
    code_str = code_str.strip()
    
    # Extract contents inside ```python ... ``` or ``` ... ```
    pattern = r"```(?:python)?\s*(.*?)\s*```"
    match = re.search(pattern, code_str, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
        
    # If starting with ``` or ending with ``` without pair
    code_str = re.sub(r"^```(?:python)?", "", code_str, flags=re.IGNORECASE)
    code_str = re.sub(r"```$", "", code_str)
    
    return code_str.strip()


def classify_error(returncode: int, stderr_str: str, timed_out: bool) -> Optional[str]:
    """Classifies subprocess execution result into our standardized error taxonomy."""
    if timed_out:
        return "timeout"
        
    if returncode == 0:
        return None
        
    stderr_lower = stderr_str.lower()
    
    if "syntaxerror" in stderr_lower or "indentationerror" in stderr_lower:
        return "syntax_error"
    elif "assertionerror" in stderr_lower:
        return "assertion_error"
    elif "modulenotfounderror" in stderr_lower or "importerror" in stderr_lower:
        return "import_error"
    elif "recursionerror" in stderr_lower:
        return "runtime_error"
    elif any(err in stderr_lower for err in ["nameerror", "typeerror", "valueerror", "indexerror", "keyerror", "zerodivisionerror", "attributeerror"]):
        return "runtime_error"
        
    return "runtime_error"


class AssertTransformer(ast.NodeTransformer):
    """AST transformer that wraps each assert statement into an isolated try/except block.
    
    This allows every individual assertion in a test suite to be executed and tracked,
    even when earlier assertions fail.
    """
    def __init__(self):
        super().__init__()
        self.assert_count = 0
        self.assert_items: List[Tuple[int, str]] = []

    def visit_Assert(self, node: ast.Assert) -> ast.AST:
        self.assert_count += 1
        aid = self.assert_count
        try:
            line_str = ast.unparse(node)
        except Exception:
            line_str = "assert"
        self.assert_items.append((aid, line_str))
        
        wrapper_code = f"""
try:
    pass
    if {aid} not in __test_records__:
        __test_records__[{aid}] = {{"test_line": {repr(line_str)}, "passed": True, "error": None}}
except BaseException as _e:
    _err_cls = type(_e).__name__
    _err_detail = str(_e).strip()
    if _err_cls == "AssertionError" and not _err_detail:
        _full_err = f"Assertion failed: {line_str}"
    elif _err_detail:
        _full_err = f"{{_err_cls}}: {{_err_detail}}"
    else:
        _full_err = f"{{_err_cls}}"
    __test_records__[{aid}] = {{"test_line": {repr(line_str)}, "passed": False, "error": _full_err}}
"""
        try_node = ast.parse(wrapper_code).body[0]
        try_node.body[0] = node
        return try_node


def extract_assertions_from_test_code(test_code: str) -> List[Tuple[int, str]]:
    """Extracts all static assertions from test code via AST, with regex fallback."""
    try:
        tree = ast.parse(test_code)
        items = []
        count = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                count += 1
                try:
                    items.append((count, ast.unparse(node)))
                except Exception:
                    items.append((count, "assert"))
        return items
    except Exception:
        matches = re.findall(r"^\s*(assert\b.*)$", test_code, re.MULTILINE)
        return [(i + 1, m.strip()) for i, m in enumerate(matches)]


def evaluate(
    generated_code: str,
    test_code: str,
    timeout_seconds: int = 5
) -> Dict[str, Any]:
    """Executes generated code against test code with per-test assertion breakdown.
    
    Returns:
        dict: {
            "passed": bool,
            "tests_total": int,
            "tests_passed": int,
            "tests_failed": int,
            "test_pass_rate": float,
            "error_type": Optional[str],
            "error_msg": str,
            "failed_test_details": List[Dict[str, str]],
            "execution_time_s": float
        }
    """
    clean_code = sanitize_generated_code(generated_code)
    static_asserts = extract_assertions_from_test_code(test_code)
    total_static = len(static_asserts) if static_asserts else 1

    # Check for empty code
    if not clean_code:
        return {
            "passed": False,
            "tests_total": total_static,
            "tests_passed": 0,
            "tests_failed": total_static,
            "test_pass_rate": 0.0,
            "error_type": "syntax_error",
            "error_msg": "Generated code is empty or missing executable code.",
            "failed_test_details": [
                {"test_line": line_str, "error": "Generated code is empty"}
                for _, line_str in static_asserts
            ] if static_asserts else [{"test_line": "empty_code", "error": "Generated code is empty"}],
            "execution_time_s": 0.0
        }

    # Fast syntax check on generated code
    try:
        ast.parse(clean_code)
    except SyntaxError as syn_err:
        err_msg = f"SyntaxError: {syn_err.msg} (line {syn_err.lineno})"
        return {
            "passed": False,
            "tests_total": total_static,
            "tests_passed": 0,
            "tests_failed": total_static,
            "test_pass_rate": 0.0,
            "error_type": "syntax_error",
            "error_msg": err_msg,
            "failed_test_details": [
                {"test_line": line_str, "error": err_msg}
                for _, line_str in static_asserts
            ] if static_asserts else [{"test_line": "code_compilation", "error": err_msg}],
            "execution_time_s": 0.0
        }

    # Create temporary results JSON and temporary execution script
    results_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    results_file_path = results_file.name
    results_file.close()

    script_file = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8")
    script_file_path = script_file.name

    transformer = AssertTransformer()
    instrumentation_success = False
    try:
        test_tree = ast.parse(test_code)
        transformed_tree = transformer.visit(test_tree)
        ast.fix_missing_locations(transformed_tree)
        
        escaped_results_path = results_file_path.replace("\\", "\\\\")
        header = ast.parse("__test_records__ = {}\n").body
        footer = ast.parse(
            f'import json\nwith open("{escaped_results_path}", "w", encoding="utf-8") as _f:\n    json.dump(__test_records__, _f)\n'
        ).body
        transformed_tree.body = header + transformed_tree.body + footer
        instrumented_test_code = ast.unparse(transformed_tree)
        full_script = f"{clean_code}\n\n# --- Test Suite ---\n{instrumented_test_code}\n"
        instrumentation_success = True
    except Exception:
        # Fallback to plain script execution if instrumentation fails
        full_script = f"{clean_code}\n\n# --- Test Suite ---\n{test_code}\n"

    script_file.write(full_script)
    script_file.close()

    start_time = time.perf_counter()
    timed_out = False
    returncode = 0
    stderr = ""
    stdout = ""

    try:
        process = subprocess.run(
            [sys.executable, script_file_path],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            encoding="utf-8",
            errors="replace"
        )
        returncode = process.returncode
        stdout = process.stdout
        stderr = process.stderr
    except subprocess.TimeoutExpired:
        timed_out = True
        returncode = -1
        stderr = f"Execution timed out after {timeout_seconds} seconds."
    except Exception as e:
        returncode = 1
        stderr = f"Subprocess runner error: {str(e)}"
    finally:
        execution_time = round(time.perf_counter() - start_time, 4)
        if os.path.exists(script_file_path):
            try:
                os.remove(script_file_path)
            except OSError:
                pass

    records: Dict[str, Dict[str, Any]] = {}
    if os.path.exists(results_file_path):
        try:
            with open(results_file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    records = json.loads(content)
        except Exception:
            records = {}
        try:
            os.remove(results_file_path)
        except OSError:
            pass

    # Handle timeout
    if timed_out:
        return {
            "passed": False,
            "tests_total": total_static,
            "tests_passed": 0,
            "tests_failed": total_static,
            "test_pass_rate": 0.0,
            "error_type": "timeout",
            "error_msg": stderr.strip(),
            "failed_test_details": [
                {"test_line": line_str, "error": "Execution timed out"}
                for _, line_str in static_asserts
            ] if static_asserts else [{"test_line": "execution", "error": "Execution timed out"}],
            "execution_time_s": execution_time
        }

    # Process records if instrumentation succeeded
    if instrumentation_success and (records or static_asserts):
        tests_passed = 0
        failed_details = []
        
        for aid, line_str in static_asserts:
            rec = records.get(str(aid)) or records.get(aid)
            if rec and rec.get("passed"):
                tests_passed += 1
            elif rec:
                failed_details.append({
                    "test_line": line_str,
                    "error": rec.get("error") or "Assertion failed"
                })
            else:
                # Unreached assertion due to prior unhandled error
                failed_details.append({
                    "test_line": line_str,
                    "error": stderr.strip() if stderr.strip() else "Test did not execute"
                })
        
        tests_total = len(static_asserts) if static_asserts else max(len(records), 1)
        tests_failed = tests_total - tests_passed
        pass_rate = round(tests_passed / tests_total, 4) if tests_total > 0 else 0.0
        all_passed = (tests_passed == tests_total and returncode == 0)
        
        error_type = None
        if not all_passed:
            if any("Assertion" in d.get("error", "") for d in failed_details):
                error_type = "assertion_error"
            else:
                error_type = classify_error(returncode if returncode != 0 else 1, stderr, False)
                
        error_msg = ""
        if not all_passed:
            if failed_details:
                error_msg = "\n".join(f"{d['test_line']} -> {d['error']}" for d in failed_details)
            elif stderr:
                error_msg = stderr.strip()

        return {
            "passed": all_passed,
            "tests_total": tests_total,
            "tests_passed": tests_passed,
            "tests_failed": tests_failed,
            "test_pass_rate": pass_rate,
            "error_type": error_type,
            "error_msg": error_msg,
            "failed_test_details": failed_details,
            "execution_time_s": execution_time
        }
    else:
        # Fallback for plain execution without assertion extraction
        passed = (returncode == 0 and not timed_out)
        err_type = classify_error(returncode, stderr, timed_out)
        return {
            "passed": passed,
            "tests_total": 1,
            "tests_passed": 1 if passed else 0,
            "tests_failed": 0 if passed else 1,
            "test_pass_rate": 1.0 if passed else 0.0,
            "error_type": err_type,
            "error_msg": stderr.strip() if not passed else "",
            "failed_test_details": [] if passed else [{"test_line": "test_suite", "error": stderr.strip()}],
            "execution_time_s": execution_time
        }
