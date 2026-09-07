"""Unit tests for src/evaluator.py to verify per-test granularity, timeouts, and error taxonomy."""

import sys
import os

# Add root directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.evaluator import evaluate, sanitize_generated_code


def test_evaluator_all_cases():
    print("=== Testing Evaluator Implementation ===")
    
    # Case 1: Valid clean code (Should PASS 100%)
    code_1 = "def add(a, b):\n    return a + b"
    test_1 = "assert add(2, 3) == 5\nassert add(-1, 1) == 0"
    res_1 = evaluate(code_1, test_1, timeout_seconds=2)
    print(f"Case 1 (Valid Code): passed={res_1['passed']}, pass_rate={res_1['test_pass_rate']}")
    assert res_1["passed"] is True
    assert res_1["error_type"] is None
    assert res_1["tests_total"] == 2
    assert res_1["tests_passed"] == 2
    assert res_1["tests_failed"] == 0
    assert res_1["test_pass_rate"] == 1.0
    assert len(res_1["failed_test_details"]) == 0
    
    # Case 2: Markdown wrapped code (Should sanitize and PASS)
    code_2 = "```python\ndef multiply(a, b):\n    return a * b\n```"
    test_2 = "assert multiply(3, 4) == 12"
    res_2 = evaluate(code_2, test_2, timeout_seconds=2)
    print(f"Case 2 (Markdown Fences): passed={res_2['passed']}, pass_rate={res_2['test_pass_rate']}")
    assert res_2["passed"] is True
    assert res_2["error_type"] is None
    assert res_2["tests_total"] == 1
    assert res_2["tests_passed"] == 1

    # Case 3: Syntax error (Should FAIL with syntax_error and 0 pass rate)
    code_3 = "def broken(\n    return 42"
    test_3 = "assert broken() == 42\nassert broken() == 42"
    res_3 = evaluate(code_3, test_3, timeout_seconds=2)
    print(f"Case 3 (Syntax Error): passed={res_3['passed']}, error_type={res_3['error_type']}, pass_rate={res_3['test_pass_rate']}")
    assert res_3["passed"] is False
    assert res_3["error_type"] == "syntax_error"
    assert res_3["test_pass_rate"] == 0.0
    assert res_3["tests_total"] == 2
    assert res_3["tests_failed"] == 2

    # Case 4: Partial pass / Partial assertion failure (2 pass, 1 fails)
    code_4 = """
def check_num(x):
    if x == 10: return False  # bug on 10
    return x > 0
"""
    test_4 = """
assert check_num(5) == True
assert check_num(10) == True
assert check_num(-2) == False
"""
    res_4 = evaluate(code_4, test_4, timeout_seconds=2)
    print(f"Case 4 (Partial Pass): passed={res_4['passed']}, passed={res_4['tests_passed']}/{res_4['tests_total']}, pass_rate={res_4['test_pass_rate']}")
    assert res_4["passed"] is False
    assert res_4["error_type"] == "assertion_error"
    assert res_4["tests_total"] == 3
    assert res_4["tests_passed"] == 2
    assert res_4["tests_failed"] == 1
    assert res_4["test_pass_rate"] == round(2/3, 4)
    assert len(res_4["failed_test_details"]) == 1
    assert "check_num(10)" in res_4["failed_test_details"][0]["test_line"]

    # Case 5: Division by Zero / Runtime error during assertion
    code_5 = "def div(a, b):\n    return a / b"
    test_5 = "assert div(10, 2) == 5\nassert div(10, 0) == 0"
    res_5 = evaluate(code_5, test_5, timeout_seconds=2)
    print(f"Case 5 (Division by Zero): passed={res_5['passed']}, pass_rate={res_5['test_pass_rate']}, error={res_5['error_type']}")
    assert res_5["passed"] is False
    assert res_5["tests_total"] == 2
    assert res_5["tests_passed"] == 1
    assert res_5["tests_failed"] == 1
    assert "ZeroDivisionError" in res_5["failed_test_details"][0]["error"]

    # Case 6: Infinite loop / Timeout (Should FAIL with timeout)
    code_6 = "def loop():\n    while True:\n        pass"
    test_6 = "assert loop() == 42"
    res_6 = evaluate(code_6, test_6, timeout_seconds=2)
    print(f"Case 6 (Infinite Loop Timeout): passed={res_6['passed']}, error_type={res_6['error_type']}, pass_rate={res_6['test_pass_rate']}")
    assert res_6["passed"] is False
    assert res_6["error_type"] == "timeout"
    assert res_6["test_pass_rate"] == 0.0

    # Case 7: HumanEval style check(candidate) with partial pass
    code_7 = """
def separate_paren_groups(paren_string: str):
    # Dummy implementation that only handles simple groups
    return ['()', '(())', '(()())']
"""
    test_7 = """
METADATA = {'author': 'jt', 'dataset': 'test'}
def check(candidate):
    assert candidate('(()()) ((())) () ((())()())') == ['(()())', '((()))', '()', '((())()())']
    assert candidate('() (()) ((())) (((())))') == ['()', '(())', '((()))', '(((())))']
    assert candidate('(()(())((())))') == ['(()(())((())))']
    assert candidate('( ) (( )) (( )( ))') == ['()', '(())', '(()())']
check(separate_paren_groups)
"""
    res_7 = evaluate(code_7, test_7, timeout_seconds=2)
    print(f"Case 7 (HumanEval Check Function): passed={res_7['passed']}, pass_rate={res_7['test_pass_rate']}, passed={res_7['tests_passed']}/{res_7['tests_total']}")
    assert res_7["passed"] is False
    assert res_7["tests_total"] == 4
    assert res_7["tests_passed"] == 1  # only the 4th assert passes with dummy implementation
    assert res_7["tests_failed"] == 3
    assert res_7["test_pass_rate"] == 0.25
    assert len(res_7["failed_test_details"]) == 3

    print("\n[SUCCESS] All 7 Evaluator test cases passed flawlessly!")


if __name__ == "__main__":
    test_evaluator_all_cases()
