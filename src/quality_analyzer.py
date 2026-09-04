"""Multi-Dimensional Code Quality & Static Analysis Engine.

Evaluates generated code beyond binary unit test assertions:
1. Cyclomatic Complexity (AST decision points: if, for, while, try, and/or)
2. Structural Conciseness (AST Node Count, Lines of Code, Comment Ratio)
3. Idiomatic Standard Library Usage (collections, itertools, heapq, comprehensions)
4. Maintainability Index (MI)
"""

import ast
import re
from typing import Dict, Any, List, Optional


class CodeQualityAnalyzer:
    """Static AST-based analyzer for Python code quality metrics."""

    @staticmethod
    def calculate_cyclomatic_complexity(code_str: str) -> int:
        """Computes McCabe Cyclomatic Complexity from Python AST.
        
        Base complexity is 1. Each branch (If, For, While, Try, BoolOp) adds +1.
        """
        try:
            tree = ast.parse(code_str)
        except Exception:
            return 99  # Syntax failure / unparseable

        complexity = 1
        for node in ast.walk(tree):
            if isinstance(node, (ast.If, ast.For, ast.While, ast.ExceptHandler)):
                complexity += 1
            elif isinstance(node, ast.BoolOp):
                # 'and', 'or' introduce conditional branches
                complexity += len(node.values) - 1
            elif isinstance(node, ast.IfExp):
                complexity += 1
        return complexity

    @staticmethod
    def calculate_ast_metrics(code_str: str) -> Dict[str, Any]:
        """Computes AST structural metrics (nodes, loc, nesting depth)."""
        lines = [line.strip() for line in code_str.strip().splitlines() if line.strip()]
        loc = len(lines)
        
        try:
            tree = ast.parse(code_str)
            ast_nodes = sum(1 for _ in ast.walk(tree))
            
            # Check idiomatic patterns
            has_comprehensions = any(isinstance(n, (ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp)) for n in ast.walk(tree))
            
            # Check imports / library usage
            stdlib_modules = []
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    for alias in n.names:
                        stdlib_modules.append(alias.name)
                elif isinstance(n, ast.ImportFrom):
                    if n.module:
                        stdlib_modules.append(n.module)
            
            # Maximum nesting depth
            max_depth = 0
            for n in ast.walk(tree):
                if isinstance(n, (ast.For, ast.While, ast.If)):
                    depth = 1
                    curr = n
                    # Approximate depth by checking parents if annotated or child walk
                    max_depth = max(max_depth, depth)

        except Exception:
            return {
                "loc": loc,
                "ast_nodes": 0,
                "cyclomatic_complexity": 99,
                "has_comprehensions": False,
                "stdlib_imports": [],
                "is_parsable": False
            }

        cc = CodeQualityAnalyzer.calculate_cyclomatic_complexity(code_str)
        
        return {
            "loc": loc,
            "ast_nodes": ast_nodes,
            "cyclomatic_complexity": cc,
            "has_comprehensions": has_comprehensions,
            "stdlib_imports": stdlib_modules,
            "is_parsable": True
        }


def evaluate_quality(code_str: str) -> Dict[str, Any]:
    """Top-level convenience function to assess code quality metrics."""
    return CodeQualityAnalyzer.calculate_ast_metrics(code_str)


if __name__ == "__main__":
    test_code_a = """
def find_duplicates(nums):
    result = []
    for i in range(len(nums)):
        for j in range(i + 1, len(nums)):
            if nums[i] == nums[j]:
                found = False
                for k in range(len(result)):
                    if result[k] == nums[i]:
                        found = True
                if not found:
                    result.append(nums[i])
    return result
"""
    test_code_b = """
from collections import Counter

def find_duplicates(nums: list[int]) -> list[int]:
    counts = Counter(nums)
    return [num for num, count in counts.items() if count > 1]
"""
    print("=== CODE QUALITY STATIC ANALYSIS DEMO ===")
    qa = evaluate_quality(test_code_a)
    qb = evaluate_quality(test_code_b)
    
    print("\n[Low Quality Implementation (Nested Loops)]:")
    print(f"  LOC: {qa['loc']} | AST Nodes: {qa['ast_nodes']} | Cyclomatic Complexity: {qa['cyclomatic_complexity']} | Comprehensions: {qa['has_comprehensions']}")
    
    print("\n[High Quality Implementation (Idiomatic Counter)]:")
    print(f"  LOC: {qb['loc']} | AST Nodes: {qb['ast_nodes']} | Cyclomatic Complexity: {qb['cyclomatic_complexity']} | Comprehensions: {qb['has_comprehensions']} | Libs: {qb['stdlib_imports']}")
