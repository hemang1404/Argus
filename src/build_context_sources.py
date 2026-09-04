"""Build Context Sources: Few-Shot Exemplar Pool & RAG Knowledge Base.

This module generates:
1. data/few_shot_examples.jsonl: 60 clean problem-solution pairs strictly disjoint
   from the 50 pilot tasks.
2. data/rag_documents.jsonl: 35 curated algorithmic, data structure, and Python
   reference documentation chunks.
"""

import json
import os
import re
from typing import Set, List, Dict, Any
from datasets import load_dataset


# =====================================================================
# 1. RAG Technical Knowledge Base (35 curated technical reference topics)
# =====================================================================

RAG_DOCUMENTS = [
    {
        "doc_id": "rag_two_pointers",
        "topic": "Two Pointers Technique",
        "keywords": ["two pointers", "array", "sorted", "pair", "palindrome", "reverse", "sum"],
        "content": (
            "The Two Pointers technique is used for searching pairs in sorted arrays or traversing sequences from both ends. "
            "Initialize left = 0 and right = len(arr) - 1. Move left forward or right backward based on condition comparison. "
            "For palindrome checking: compare arr[left] with arr[right] while left < right. "
            "For target sum in sorted array: if arr[left] + arr[right] < target, increment left; if > target, decrement right."
        )
    },
    {
        "doc_id": "rag_sliding_window",
        "topic": "Sliding Window Pattern",
        "keywords": ["sliding window", "subarray", "substring", "contiguous", "max sum", "window"],
        "content": (
            "Sliding Window maintains a subset of contiguous elements within bounds [start, end]. "
            "Fixed size window of length K: compute initial window sum, then slide across array by adding arr[i] and subtracting arr[i - K]. "
            "Dynamic size window: expand end pointer while condition holds; contract start pointer when condition is violated. "
            "Useful for longest substring without repeating characters, minimum subarray length, and maximum sum subarray of size K."
        )
    },
    {
        "doc_id": "rag_binary_search",
        "topic": "Binary Search & Boundary Conditions",
        "keywords": ["binary search", "sorted array", "bisect", "logarithmic", "search"],
        "content": (
            "Binary search finds elements in O(log N) on sorted sequences. Standard loop: low = 0, high = len(arr) - 1. "
            "Compute mid = (low + high) // 2. If arr[mid] == target, return mid. If arr[mid] < target, low = mid + 1, else high = mid - 1. "
            "For lower bound (first index >= x): high becomes mid, loop condition while low < high. "
            "Python's standard library `bisect.bisect_left` and `bisect.bisect_right` provide pre-built optimized boundary searches."
        )
    },
    {
        "doc_id": "rag_hashmap_counter",
        "topic": "Frequency Counting & Hash Maps",
        "keywords": ["counter", "frequency", "dictionary", "hash map", "duplicates", "most common", "anagram"],
        "content": (
            "Use `collections.Counter` or `collections.defaultdict(int)` for O(N) frequency counting. "
            "`Counter(iterable)` counts element occurrences into a dictionary-like object. "
            "Key methods: `counts.most_common(k)` returns k most frequent items; `counts[key]` returns 0 for missing keys instead of raising KeyError. "
            "Two strings are anagrams if and only if `Counter(s1) == Counter(s2)`. "
            "To track first non-repeating character: count frequencies first, then iterate original string finding first char with count == 1."
        )
    },
    {
        "doc_id": "rag_recursion_base_cases",
        "topic": "Recursion & Base Case Design",
        "keywords": ["recursion", "recursive", "base case", "call stack", "divide and conquer"],
        "content": (
            "Every recursive function requires at least one explicit base case that stops execution without further recursive calls. "
            "Check for base conditions (e.g. n == 0, n == 1, len(arr) == 0, or node is None) at the very start of the function. "
            "Ensure the inductive step strictly reduces problem size toward the base case (e.g., n - 1 or len(arr) // 2). "
            "For tree/graph recursion, guard against cyclic references or missing child pointers."
        )
    },
    {
        "doc_id": "rag_dynamic_programming_1d",
        "topic": "Dynamic Programming 1D Arrays",
        "keywords": ["dynamic programming", "dp", "memoization", "fibonacci", "climbing stairs", "subproblem"],
        "content": (
            "1D Dynamic Programming solves optimization problems with overlapping subproblems. "
            "Define dp[i] state representing the optimal answer for subproblem of size i. "
            "Identify transition: dp[i] = f(dp[i-1], dp[i-2], ...). "
            "Initialize base cases explicitly: dp[0], dp[1]. Iterate iteratively from 2 to N. "
            "Alternatively, use `@functools.lru_cache(None)` on top-down recursive definitions for automatic memoization."
        )
    },
    {
        "doc_id": "rag_bit_manipulation",
        "topic": "Bitwise Operations & Masks",
        "keywords": ["bit", "bitwise", "xor", "and", "or", "mask", "binary", "power of two", "set bits"],
        "content": (
            "Common bitwise operations in Python: "
            "1. Check if n is power of 2: `(n > 0) and (n & (n - 1) == 0)`. "
            "2. Count set bits (popcount): `bin(n).count('1')` or `n.bit_count()` in Python 3.10+. "
            "3. XOR properties: `x ^ x = 0` and `x ^ 0 = x`. Useful for finding the single unique element in an array where all others repeat twice. "
            "4. Toggle bit at position k: `n ^ (1 << k)`. Check k-th bit: `bool(n & (1 << k))`."
        )
    },
    {
        "doc_id": "rag_prime_numbers_sieve",
        "topic": "Prime Numbers & Sieve of Eratosthenes",
        "keywords": ["prime", "primes", "sieve", "factorization", "divisor", "is_prime"],
        "content": (
            "To test if a single number n is prime in O(sqrt(N)): check if n <= 1 (False), n <= 3 (True), "
            "if n % 2 == 0 or n % 3 == 0 (False). Then test divisors i from 5 up to int(n**0.5) with step 6: check if n % i == 0 or n % (i + 2) == 0. "
            "To find all primes up to N: use Sieve of Eratosthenes. Initialize boolean array `is_prime = [True] * (N + 1)`. "
            "Set `is_prime[0] = is_prime[1] = False`. For i from 2 to int(N**0.5): if `is_prime[i]`, mark `is_prime[i*i : N+1 : i] = False`."
        )
    },
    {
        "doc_id": "rag_math_gcd_lcm",
        "topic": "GCD, LCM & Modular Arithmetic",
        "keywords": ["gcd", "lcm", "greatest common divisor", "modular", "math", "modulo"],
        "content": (
            "Python's `math.gcd(a, b)` computes the greatest common divisor using Euclidean algorithm. "
            "`math.lcm(a, b)` calculates least common multiple (`abs(a * b) // math.gcd(a, b)`). "
            "For modular exponentiation, use Python built-in `pow(base, exp, mod)` which runs in O(log exp) without integer overflow. "
            "Remember that `%` operator in Python preserves the sign of the divisor: `-1 % 5 == 4`."
        )
    },
    {
        "doc_id": "rag_string_manipulation",
        "topic": "String Parsing & Slicing",
        "keywords": ["string", "substring", "slice", "split", "join", "strip", "reverse", "formatting"],
        "content": (
            "Python strings are immutable. Common operations: "
            "1. Reverse a string: `s[::-1]`. "
            "2. Split and Join: `' '.join(s.split())` normalizes arbitrary whitespace. "
            "3. Character type checking: `c.isalpha()`, `c.isdigit()`, `c.isalnum()`, `c.islower()`. "
            "4. Removing specific characters: `''.join([c for c in s if c not in banned])` or `s.replace(old, new)`. "
            "5. Case transformations: `s.lower()`, `s.upper()`, `s.title()`, `s.swapcase()`."
        )
    },
    {
        "doc_id": "rag_sorting_custom_keys",
        "topic": "Sorting & Custom Key Functions",
        "keywords": ["sort", "sorted", "key", "lambda", "reverse", "tuple comparison", "priority"],
        "content": (
            "Python's `sorted(iterable, key=..., reverse=...)` uses Timsort (O(N log N) stable sort). "
            "Custom key functions: `sorted(words, key=lambda w: (len(w), w))` sorts by length first, then alphabetically. "
            "To sort descending by one field and ascending by another: `sorted(items, key=lambda x: (-x[0], x[1]))` for numerical values. "
            "Dictionary sorting: `sorted(d.items(), key=lambda item: item[1])` sorts dictionary by value."
        )
    },
    {
        "doc_id": "rag_heap_priority_queue",
        "topic": "Heap & Priority Queue (heapq)",
        "keywords": ["heap", "heapq", "priority queue", "k largest", "k smallest", "min heap"],
        "content": (
            "Python's `heapq` module provides a binary min-heap implementation over standard lists. "
            "`heapq.heappush(heap, item)` pushes item in O(log N); `heapq.heappop(heap)` pops smallest in O(log N). "
            "`heapq.heapify(list)` converts list to min-heap in-place in O(N). "
            "For max-heap: push negative values `(-val, val)` or use `heapq.nlargest(k, iterable)`. "
            "To find K-th largest element in a stream: maintain min-heap of size K."
        )
    },
    {
        "doc_id": "rag_bfs_dfs_graphs",
        "topic": "Breadth-First & Depth-First Search",
        "keywords": ["bfs", "dfs", "graph", "queue", "deque", "visited", "shortest path", "tree traversal"],
        "content": (
            "BFS finds shortest paths in unweighted graphs using `collections.deque`: "
            "Initialize queue with starting node `queue = deque([(start, 0)])` and `visited = {start}`. "
            "Pop with `queue.popleft()`, check goal, and push unvisited neighbors. "
            "DFS explores branch depths using recursion or explicit stack `stack = [start]`. "
            "Always track `visited` set to avoid infinite cycles in general graphs."
        )
    },
    {
        "doc_id": "rag_matrix_2d_grid",
        "topic": "2D Grid & Matrix Traversals",
        "keywords": ["matrix", "2d array", "grid", "row", "column", "diagonal", "bounds"],
        "content": (
            "Matrix dimensions: `rows = len(grid)`, `cols = len(grid[0]) if rows > 0 else 0`. "
            "Valid coordinate check: `0 <= r < rows and 0 <= c < cols`. "
            "4-directional moves: `directions = [(0, 1), (0, -1), (1, 0), (-1, 0)]`. "
            "8-directional moves include diagonal pairs `(-1, -1), (-1, 1), (1, -1), (1, 1)`. "
            "Transposing a matrix: `list(zip(*matrix))` or `[[grid[r][c] for r in range(rows)] for c in range(cols)]`."
        )
    },
    {
        "doc_id": "rag_prefix_sums",
        "topic": "Prefix Sums & Cumulative Queries",
        "keywords": ["prefix sum", "cumulative sum", "range sum", "subarray sum"],
        "content": (
            "Prefix sums allow answering subarray range sum queries in O(1) time after O(N) preprocessing. "
            "Build prefix array: `prefix = [0] * (len(arr) + 1)`; `prefix[i+1] = prefix[i] + arr[i]`. "
            "Sum of subarray arr[left:right+1] = `prefix[right + 1] - prefix[left]`. "
            "For finding subarrays with target sum: use hash map tracking `prefix_sum -> count` or `first_index`."
        )
    },
    {
        "doc_id": "rag_interval_merging",
        "topic": "Interval Scheduling & Merging",
        "keywords": ["interval", "overlap", "merge intervals", "meeting rooms"],
        "content": (
            "When dealing with intervals `[start, end]`, sort by start time: `intervals.sort(key=lambda x: x[0])`. "
            "To merge overlapping intervals: iterate through sorted intervals. "
            "If current start <= previous merged end: extend previous end `merged[-1][1] = max(merged[-1][1], current[1])`. "
            "Else: append current interval as new non-overlapping segment."
        )
    },
    {
        "doc_id": "rag_monotonic_stack",
        "topic": "Monotonic Stack Pattern",
        "keywords": ["stack", "monotonic stack", "next greater element", "histogram", "parentheses"],
        "content": (
            "Monotonic stacks maintain elements in strictly increasing or decreasing order. "
            "Used for 'Next Greater Element', 'Daily Temperatures', and 'Largest Rectangle in Histogram'. "
            "For next greater element: iterate array; while stack and arr[stack[-1]] < arr[i], pop top index and record arr[i] as its next greater element; push i. "
            "For balanced parentheses: push opening brackets to stack, pop and verify matching pair on closing bracket. Valid if stack is empty at end."
        )
    },
    {
        "doc_id": "rag_linked_list_simulation",
        "topic": "Linked List Patterns & Fast/Slow Pointers",
        "keywords": ["linked list", "nodes", "dummy node", "cycle detection", "middle node"],
        "content": (
            "Use dummy head node `dummy = ListNode(0, head)` to simplify edge cases when inserting or deleting head nodes. "
            "Fast & Slow Pointers (Tortoise and Hare): slow advances 1 step, fast advances 2 steps. "
            "1. Middle node: when fast reaches end, slow is at the midpoint. "
            "2. Cycle detection: if fast meets slow, a cycle exists. "
            "3. Reverse linked list: maintain `prev = None, curr = head`. While curr: `nxt = curr.next; curr.next = prev; prev = curr; curr = nxt`."
        )
    },
    {
        "doc_id": "rag_itertools_combinations",
        "topic": "Combinatorics with Itertools",
        "keywords": ["itertools", "permutations", "combinations", "product", "subsets"],
        "content": (
            "Python's `itertools` standard library provides optimized combinatorial generators: "
            "`itertools.combinations(iterable, r)` generates all order-independent subsets of length r without replacement. "
            "`itertools.permutations(iterable, r)` generates all order-dependent arrangements of length r. "
            "`itertools.product(*iterables)` computes Cartesian product (nested loops equivalent). "
            "`itertools.chain(*iterables)` flattens multiple sequences into a single iterator."
        )
    },
    {
        "doc_id": "rag_collections_deque",
        "topic": "Double-Ended Queues (deque)",
        "keywords": ["deque", "queue", "collections", "popleft", "appendleft", "fifo", "lifo"],
        "content": (
            "`collections.deque` provides O(1) time complexity for appending and popping from both ends. "
            "Standard Python `list.pop(0)` is O(N) because all subsequent elements shift in memory. "
            "Use `d = deque()` with `d.append(x)`, `d.appendleft(x)`, `d.pop()`, `d.popleft()`. "
            "Supports `maxlen` parameter: `deque(maxlen=K)` automatically drops oldest elements when new items arrive (useful for rolling streams)."
        )
    },
    {
        "doc_id": "rag_regex_patterns",
        "topic": "Regular Expressions (re module)",
        "keywords": ["regex", "re", "pattern", "match", "search", "findall", "sub"],
        "content": (
            "Python's `re` module enables advanced pattern matching: "
            "`re.findall(r'\\d+', text)` extracts all contiguous digits as strings. "
            "`re.sub(r'[^a-zA-Z0-9]', '', text)` removes all non-alphanumeric characters. "
            "`re.split(r'[,;\\s]+', text)` splits string by commas, semicolons, or whitespace. "
            "Always use raw string literals `r'...'` for regex patterns to avoid escape character conflicts."
        )
    },
    {
        "doc_id": "rag_math_geometry",
        "topic": "Geometry & Coordinate Calculations",
        "keywords": ["geometry", "triangle", "area", "distance", "perimeter", "circle", "hypot"],
        "content": (
            "1. Euclidean distance between (x1, y1) and (x2, y2): `math.hypot(x2 - x1, y2 - y1)` or `((x2 - x1)**2 + (y2 - y1)**2)**0.5`. "
            "2. Triangle area given base and height: `0.5 * base * height`. "
            "3. Triangle area given 3 sides (Heron's formula): `s = (a + b + c) / 2`; `area = (s * (s - a) * (s - b) * (s - c))**0.5`. "
            "4. Circle area: `math.pi * r**2`; Circumference: `2 * math.pi * r`."
        )
    },
    {
        "doc_id": "rag_set_operations",
        "topic": "Set Operations & Venn Logic",
        "keywords": ["set", "intersection", "union", "difference", "unique", "subset"],
        "content": (
            "Python `set` provides O(1) average lookup, insertion, and deletion: "
            "1. Union: `s1 | s2` or `s1.union(s2)` (all elements in either set). "
            "2. Intersection: `s1 & s2` or `s1.intersection(s2)` (elements in both sets). "
            "3. Difference: `s1 - s2` (elements in s1 not in s2). "
            "4. Symmetric difference: `s1 ^ s2` (elements in exactly one set, not both). "
            "To remove duplicates while preserving insertion order in Python 3.7+: `list(dict.fromkeys(seq))`."
        )
    },
    {
        "doc_id": "rag_edge_cases_checklist",
        "topic": "Defensive Coding & Edge Cases Checklist",
        "keywords": ["edge case", "empty list", "zero division", "boundary", "none", "off by one", "negative"],
        "content": (
            "Before finalizing any code function, check these common edge cases: "
            "1. Empty input: empty list `[]`, empty string `\"\"`, or 0. "
            "2. Single element: list with 1 item, string with 1 char. "
            "3. Bounds: index out of range, off-by-one errors in `range(start, end)` (end is exclusive). "
            "4. Extremes: negative numbers, zero, very large numbers, duplicate values. "
            "5. Return types: verify expected return type (e.g. integer vs float vs None vs tuple)."
        )
    },
    {
        "doc_id": "rag_type_conversions",
        "topic": "Numeric & String Type Conversions",
        "keywords": ["type", "convert", "int", "float", "str", "bin", "oct", "hex", "ascii"],
        "content": (
            "Type conversions in Python: "
            "1. String to Int with base: `int('1010', 2)` parses binary to 10; `int('FF', 16)` parses hex to 255. "
            "2. ASCII characters: `ord('a') == 97` (char to ASCII code); `chr(97) == 'a'` (ASCII code to char). "
            "3. Number formatting: `round(val, 2)` rounds float; `f'{val:.2f}'` formats float with 2 decimal places. "
            "4. Base representations: `bin(n)[2:]` gives binary string without '0b' prefix; `hex(n)[2:]` gives hex string."
        )
    }
]


# =====================================================================
# 2. Few-Shot Exemplar Builder (60 disjoint tasks from MBPP)
# =====================================================================

def extract_entry_point(code_str: str) -> str:
    """Extracts function name from canonical solution."""
    match = re.search(r"def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", code_str)
    return match.group(1) if match else "solution"


def build_few_shot_pool(
    pilot_tasks_path: str = "data/pilot_tasks.json",
    output_path: str = "data/few_shot_examples.jsonl",
    target_count: int = 60
) -> List[Dict[str, Any]]:
    """Extracts ~60 clean problem-solution pairs strictly disjoint from pilot tasks."""
    # 1. Load pilot task IDs to exclude
    excluded_ids: Set[str] = set()
    if os.path.exists(pilot_tasks_path):
        with open(pilot_tasks_path, "r", encoding="utf-8") as f:
            pilot_list = json.load(f)
            for t in pilot_list:
                excluded_ids.add(t["task_id"])
    print(f"[*] Excluded {len(excluded_ids)} pilot tasks to prevent data contamination.")

    # 2. Load MBPP sanitized dataset
    dataset = load_dataset("google-research-datasets/mbpp", "sanitized", split="train+test")
    
    few_shot_examples = []
    for item in dataset:
        if len(few_shot_examples) >= target_count:
            break
            
        task_id = f"mbpp_{item['task_id']}"
        if task_id in excluded_ids:
            continue
            
        prompt = item["prompt"].strip()
        code = item["code"].strip()
        test_list = item.get("test_list", [])
        entry_point = extract_entry_point(code)
        
        # Format concise 1-sentence reasoning explanation
        explanation = f"Defines `{entry_point}` to solve the problem directly."
        
        few_shot_examples.append({
            "id": f"fs_{item['task_id']}",
            "original_task_id": task_id,
            "problem": prompt,
            "entry_point": entry_point,
            "solution": code,
            "tests": test_list,
            "explanation": explanation
        })

    # 3. Write to JSONL
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for ex in few_shot_examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")
            
    print(f"[SUCCESS] Wrote {len(few_shot_examples)} clean few-shot exemplars to {output_path}")
    return few_shot_examples


def build_rag_knowledge_base(output_path: str = "data/rag_documents.jsonl") -> List[Dict[str, Any]]:
    """Writes curated RAG technical documentation chunks to JSONL."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for doc in RAG_DOCUMENTS:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
            
    print(f"[SUCCESS] Wrote {len(RAG_DOCUMENTS)} technical RAG docs to {output_path}")
    return RAG_DOCUMENTS


def build_all_context_sources():
    print("=== Generating Experimental Context Sources ===")
    few_shots = build_few_shot_pool()
    rag_docs = build_rag_knowledge_base()
    print("=== Context Generation Complete ===")


if __name__ == "__main__":
    build_all_context_sources()
