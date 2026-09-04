"""Unit tests for src/evaluator.py to verify timeout and error taxonomy classification."""

import sys
import os

# Add root directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.evaluator import evaluate, sanitize_generated_code


def test_evaluator_all_cases():
    print("=== Testing Evaluator Implementation ===")
    
    # Case 1: Valid clean code (Should PASS)
    code_1 = "def add(a, b):\n    return a + b"
    test_1 = "assert add(2, 3) == 5\nassert add(-1, 1) == 0"
    res_1 = evaluate(code_1, test_1, timeout_seconds=2)
    print(f"Case 1 (Valid Code): passed={res_1['passed']}, error_type={res_1['error_type']}")
    assert res_1["passed"] is True
    assert res_1["error_type"] is None
    
    # Case 2: Markdown wrapped code (Should sanitize and PASS)
    code_2 = "```python\ndef multiply(a, b):\n    return a * b\n```"
    test_2 = "assert multiply(3, 4) == 12"
    res_2 = evaluate(code_2, test_2, timeout_seconds=2)
    print(f"Case 2 (Markdown Fences): passed={res_2['passed']}, error_type={res_2['error_type']}")
    assert res_2["passed"] is True
    assert res_2["error_type"] is None

    # Case 3: Syntax error (Should FAIL with syntax_error)
    code_3 = "def broken(\n    return 42"
    test_3 = "assert broken() == 42"
    res_3 = evaluate(code_3, test_3, timeout_seconds=2)
    print(f"Case 3 (Syntax Error): passed={res_3['passed']}, error_type={res_3['error_type']}")
    assert res_3["passed"] is False
    assert res_3["error_type"] == "syntax_error"

    # Case 4: Assertion failure (Should FAIL with assertion_error)
    code_4 = "def sub(a, b):\n    return a + b  # Bug: returns addition instead of subtraction"
    test_4 = "assert sub(5, 2) == 3"
    res_4 = evaluate(code_4, test_4, timeout_seconds=2)
    print(f"Case 4 (Assertion Failure): passed={res_4['passed']}, error_type={res_4['error_type']}")
    assert res_4["passed"] is False
    assert res_4["error_type"] == "assertion_error"

    # Case 5: Runtime error (Should FAIL with runtime_error)
    code_5 = "def div(a, b):\n    return a / b"
    test_5 = "assert div(10, 0) == 0"
    res_5 = evaluate(code_5, test_5, timeout_seconds=2)
    print(f"Case 5 (Division by Zero): passed={res_5['passed']}, error_type={res_5['error_type']}")
    assert res_5["passed"] is False
    assert res_5["error_type"] == "runtime_error"

    # Case 6: Infinite loop / Timeout (Should FAIL with timeout)
    code_6 = "def loop():\n    while True:\n        pass"
    test_6 = "loop()"
    res_6 = evaluate(code_6, test_6, timeout_seconds=2)
    print(f"Case 6 (Infinite Loop Timeout): passed={res_6['passed']}, error_type={res_6['error_type']}")
    assert res_6["passed"] is False
    assert res_6["error_type"] == "timeout"
    
    print("\n[SUCCESS] All 6 Evaluator test cases passed flawlessly!")


if __name__ == "__main__":
    test_evaluator_all_cases()
