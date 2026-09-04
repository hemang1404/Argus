"""Evaluator Module for AI-Generated Code Execution.

Executes code against test suites in an isolated OS subprocess with strict timeouts
and detailed error taxonomy classification.
"""

import os
import re
import sys
import tempfile
import time
import subprocess
from typing import Dict, Any, Optional


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


def evaluate(
    generated_code: str,
    test_code: str,
    timeout_seconds: int = 5
) -> Dict[str, Any]:
    """Executes generated code against test code in a separate Python subprocess.
    
    Returns:
        dict: {
            "passed": bool,
            "error_type": Optional[str],
            "error_msg": str,
            "execution_time_s": float
        }
    """
    clean_code = sanitize_generated_code(generated_code)
    
    # Combine user solution and test assertions
    full_script = f"{clean_code}\n\n# --- Test Suite ---\n{test_code}\n"
    
    # Create temporary file to execute
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as temp_file:
        temp_file.write(full_script)
        temp_file_path = temp_file.name
        
    start_time = time.perf_counter()
    timed_out = False
    returncode = 0
    stdout = ""
    stderr = ""
    
    try:
        process = subprocess.run(
            [sys.executable, temp_file_path],
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
        execution_time = time.perf_counter() - start_time
        # Clean up temporary execution script
        if os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except OSError:
                pass
                
    passed = (returncode == 0 and not timed_out)
    error_type = classify_error(returncode, stderr, timed_out)
    
    return {
        "passed": passed,
        "error_type": error_type,
        "error_msg": stderr.strip() if not passed else "",
        "execution_time_s": round(execution_time, 4)
    }
