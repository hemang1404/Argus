"""Task suite for the Argus end-to-end package test.

Each task is a tiny single-file Python project with ONE failing visible test and a stronger hidden test
that the model never sees. The hidden test measures whether an accepted ("verified") patch is actually
correct (the same verifier-strength question as in the SWE-bench study).

expect:
  fix       -> a correct fix exists; SUCCESS is the expected status
  already   -> the visible test already passes; expected status ALREADY_PASSING, no model call
  impossible-> the visible tests contradict each other; a SUCCESS here would be a false verification
"""

from __future__ import annotations

TASKS = [
    dict(
        id="t01_off_by_one", category="logic", expect="fix",
        src="def last_n(xs, n):\n    \"\"\"Return the last n items of xs.\"\"\"\n    return xs[len(xs) - n - 1:]\n",
        test="from src.mod import last_n\n\n\ndef test_last_two():\n    assert last_n([1, 2, 3, 4], 2) == [3, 4]\n",
        hidden="from src.mod import last_n\n\n\ndef test_zero():\n    assert last_n([1, 2, 3], 0) == []\n\n\ndef test_longer_than_list():\n    assert last_n([1, 2], 5) == [1, 2]\n",
    ),
    dict(
        id="t02_operator", category="logic", expect="fix",
        src="def is_adult(age):\n    return age > 18\n",
        test="from src.mod import is_adult\n\n\ndef test_boundary():\n    assert is_adult(18) is True\n",
        hidden="from src.mod import is_adult\n\n\ndef test_minor():\n    assert is_adult(17) is False\n\n\ndef test_adult():\n    assert is_adult(40) is True\n",
    ),
    dict(
        id="t03_nested_indent", category="indentation", expect="fix",
        src=(
            "def first_pair_with_sum(nums, target):\n"
            "    for i in range(len(nums)):\n"
            "        for j in range(i, len(nums)):\n"
            "            if nums[i] + nums[j] == target:\n"
            "                return (i, j)\n"
            "    return None\n"
        ),
        test="from src.mod import first_pair_with_sum\n\n\ndef test_distinct_indices():\n    assert first_pair_with_sum([3, 5, 1], 6) == (0, 1) or first_pair_with_sum([3, 5, 1], 6) == (1, 2)\n    assert first_pair_with_sum([3], 6) is None\n",
        hidden="from src.mod import first_pair_with_sum\n\n\ndef test_same_element_not_reused():\n    assert first_pair_with_sum([3, 4], 6) is None\n\n\ndef test_pair():\n    assert first_pair_with_sum([2, 7, 11, 15], 9) == (0, 1)\n",
    ),
    dict(
        id="t04_crlf_file", category="line-endings", expect="fix", src_eol="\r\n",
        src="def add(a, b):\r\n    return a - b\r\n",
        test="from src.mod import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        hidden="from src.mod import add\n\n\ndef test_add_negative():\n    assert add(-2, -3) == -5\n",
    ),
    dict(
        id="t05_bom_unicode", category="encoding", expect="fix", src_bom=True,
        src="def greet(name):\n    \"\"\"Greet a user: café style.\"\"\"\n    return \"Hello \" + name.upper()\n",
        test="from src.mod import greet\n\n\ndef test_greet():\n    assert greet(\"Ana\") == \"Hello Ana\"\n",
        hidden="from src.mod import greet\n\n\ndef test_greet_keeps_case():\n    assert greet(\"zoë\") == \"Hello zoë\"\n",
    ),
    dict(
        id="t06_two_hunks", category="multi-edit", expect="fix",
        src=(
            "def mean(xs):\n"
            "    return sum(xs) / (len(xs) + 1)\n"
            "\n"
            "\n"
            "def variance(xs):\n"
            "    m = mean(xs)\n"
            "    return sum((x - m) ** 2 for x in xs) / (len(xs) + 1)\n"
        ),
        test="from src.mod import mean, variance\n\n\ndef test_stats():\n    assert mean([2, 4, 6]) == 4\n    assert variance([2, 4, 6]) == 8 / 3\n",
        hidden="from src.mod import mean, variance\n\n\ndef test_single():\n    assert mean([5]) == 5\n    assert variance([5]) == 0\n",
    ),
    dict(
        id="t07_title_case", category="strings", expect="fix",
        src="def title(s):\n    return s.upper()\n",
        test="from src.mod import title\n\n\ndef test_title():\n    assert title(\"hello world\") == \"Hello World\"\n",
        hidden="from src.mod import title\n\n\ndef test_extra_spaces():\n    assert title(\"a  b\") == \"A  B\"\n\n\ndef test_empty():\n    assert title(\"\") == \"\"\n",
    ),
    dict(
        id="t08_mutable_default", category="semantics", expect="fix",
        src="def add_item(x, items=[]):\n    items.append(x)\n    return items\n",
        test="from src.mod import add_item\n\n\ndef test_fresh_list_each_call():\n    assert add_item(1) == [1]\n    assert add_item(2) == [2]\n",
        hidden="from src.mod import add_item\n\n\ndef test_explicit_list_is_used():\n    base = [9]\n    assert add_item(1, base) == [9, 1]\n    assert base == [9, 1]\n",
    ),
    dict(
        id="t09_dict_merge", category="semantics", expect="fix",
        src="def merge(defaults, overrides):\n    result = defaults\n    result.update(overrides)\n    return result\n",
        test="from src.mod import merge\n\n\ndef test_merge_does_not_mutate():\n    d = {\"a\": 1}\n    assert merge(d, {\"b\": 2}) == {\"a\": 1, \"b\": 2}\n    assert d == {\"a\": 1}\n",
        hidden="from src.mod import merge\n\n\ndef test_override_wins():\n    assert merge({\"a\": 1}, {\"a\": 5}) == {\"a\": 5}\n",
    ),
    dict(
        id="t10_missing_import", category="runtime-error", expect="fix",
        src="def hypot(a, b):\n    return math.sqrt(a * a + b * b)\n",
        test="from src.mod import hypot\n\n\ndef test_hypot():\n    assert hypot(3, 4) == 5\n",
        hidden="from src.mod import hypot\n\n\ndef test_zero():\n    assert hypot(0, 0) == 0\n",
    ),
    dict(
        id="t11_already_passing", category="control", expect="already",
        src="def double(x):\n    return x * 2\n",
        test="from src.mod import double\n\n\ndef test_double():\n    assert double(4) == 8\n",
        hidden="from src.mod import double\n\n\ndef test_double_zero():\n    assert double(0) == 0\n",
    ),
    dict(
        id="t12_contradictory_tests", category="impossible", expect="impossible",
        src="def f(x):\n    return x * 2\n",
        test="from src.mod import f\n\n\ndef test_a():\n    assert f(2) == 4\n\n\ndef test_b():\n    assert f(2) == 5\n",
        hidden="from src.mod import f\n\n\ndef test_a_hidden():\n    assert f(2) == 4\n",
    ),
]
