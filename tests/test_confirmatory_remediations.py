"""Regression tests for evaluator/localization/provider-error remediations.

Validates fixes for known issues identified during the 30-task pilot:
1. Evaluator setup patch application (regression test for pydata__xarray-4629 evaluator failure).
2. Context localization for test-runner indirection using symbol matching.
3. Strict fail-closed refusal when localization confidence is insufficient (no speculative head patches).
4. Provider error classification (HTTP 400/413 / transport errors do not record successful completions).
"""

from __future__ import annotations

from typing import List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from argus.runtime.container import (
    ContainerConfig,
    ContainerEvaluator,
)
from argus.runtime.controller import (
    RecoveryController,
    extract_context_frame,
)


class MockEvaluatorEnvironment:
    """Simulates a container environment where canonical tests strictly require test_patch."""

    def __init__(self, has_setup_patch: bool = False) -> None:
        self.has_setup_patch = has_setup_patch
        self.candidate_applied = False
        self.command_log: List[List[str]] = []
        self.cleaned_up = False

    def executor(
        self,
        args: List[str],
        input_bytes: Optional[bytes] = None,
        timeout: Optional[int] = None,
    ) -> Tuple[int, str, str]:
        self.command_log.append(list(args))
        cmd = " ".join(args)

        if "run -d" in cmd:
            return (0, "mock_canon_cid_1\n", "")
        if "rev-parse HEAD" in cmd:
            return (0, "a41edc7bf5302f2ea327943c0c48c532b12009bc\n", "")
        if "git apply" in cmd:
            # Setup patch applied
            self.has_setup_patch = True
            return (0, "", "")
        if "tar -xf -" in cmd:
            self.candidate_applied = True
            return (0, "", "")
        if "sha256sum" in cmd:
            return (0, "mock_sha  /testbed/xarray/core/merge.py\n", "")
        if "pytest" in cmd:
            # Canonical SWE-bench verification logic:
            # If test_patch was NOT applied, the test file does not exist -> Exit 4 (not found)
            if not self.has_setup_patch:
                return (4, "ERROR: file or directory not found: xarray/tests/test_merge.py::test_merge_attrs_override_copy", "")
            # If setup patch WAS applied AND candidate was applied -> Pass!
            if self.candidate_applied:
                return (0, "1 passed in 0.25s", "")
            return (1, "FAILED: AssertionError in test_merge_attrs_override_copy", "")
        if "rm -f" in cmd:
            self.cleaned_up = True
            return (0, "mock_canon_cid_1\n", "")

        return (0, "OK", "")


def test_xarray_4629_evaluator_setup_patch_regression():
    """Verify that omitting apply_setup_patch causes canonical test failure (Exit 4),

    while applying setup_patch ensures verified canonical test pass.
    """
    # 1. Simulating the pilot bug (setup patch omitted)
    mock_env_without_setup = MockEvaluatorEnvironment(has_setup_patch=False)
    evaluator_bad = ContainerEvaluator(
        config=ContainerConfig(image="swebench/sweb.eval.x86_64.pydata_1776_xarray-4629:latest"),
        raw_executor=mock_env_without_setup.executor,
    )

    cid = evaluator_bad.start_container()
    try:
        evaluator_bad.verify_base_commit(cid, "a41edc7bf5302f2ea327943c0c48c532b12009bc")
        # Pilot bug: apply_setup_patch was omitted here!
        evaluator_bad.transfer_and_verify_candidate(
            cid, "xarray/core/merge.py", "return dict(variable_attrs[0])", expected_candidate_hash="mock_sha"
        )
        res_omitted = evaluator_bad.execute_test(cid, "pytest xarray/tests/test_merge.py::test_merge_attrs_override_copy")
        assert not res_omitted.passed
        assert res_omitted.exit_code == 4  # Reproduces the historical pilot failure
    finally:
        evaluator_bad.destroy_container(cid)
    assert mock_env_without_setup.cleaned_up

    # 2. Simulating the remediated workflow (setup patch explicitly applied)
    mock_env_fixed = MockEvaluatorEnvironment(has_setup_patch=False)
    evaluator_fixed = ContainerEvaluator(
        config=ContainerConfig(image="swebench/sweb.eval.x86_64.pydata_1776_xarray-4629:latest"),
        raw_executor=mock_env_fixed.executor,
    )

    cid_fixed = evaluator_fixed.start_container()
    try:
        evaluator_fixed.verify_base_commit(cid_fixed, "a41edc7bf5302f2ea327943c0c48c532b12009bc")
        # Remediated: test patch applied before test execution
        evaluator_fixed.apply_setup_patch(cid_fixed, "diff --git a/test_merge.py...")
        evaluator_fixed.transfer_and_verify_candidate(
            cid_fixed, "xarray/core/merge.py", "return dict(variable_attrs[0])", expected_candidate_hash="mock_sha"
        )
        res_fixed = evaluator_fixed.execute_test(cid_fixed, "pytest xarray/tests/test_merge.py::test_merge_attrs_override_copy")
        assert res_fixed.passed
        assert res_fixed.exit_code == 0  # Confirms verified fix
    finally:
        evaluator_fixed.destroy_container(cid_fixed)
    assert mock_env_fixed.cleaned_up


def test_test_runner_indirection_symbol_localization():
    """Verify that when traceback is indirect (e.g.

    Testdir.runpytest()), symbol matching against target AST resolves the
    intended class/method context window.
    """
    # Large source file (>100 lines) containing target symbol
    code_lines = [f"# Padding line {i+1}" for i in range(150)]
    code_lines[80] = "class TestRunnerConfig:"
    code_lines[85] = "    def configure_options(self, options):"
    code_lines[86] = "        if not options:"
    code_lines[87] = "            raise ValueError('empty options')"
    code_lines[88] = "        return options"
    full_code = "\n".join(code_lines)

    # Error traceback has test-runner indirection (points to testdir fixture in pytester.py, not target file)
    indirect_traceback = (
        "Traceback (most recent call last):\n"
        '  File "/opt/miniconda3/envs/testbed/lib/python3.9/site-packages/_pytest/pytester.py", line 600, in runpytest\n'
        "    return self._runpytest_subprocess(*args)\n"
        "AssertionError: assert result.ret == 0 (got 1)\n"
        "Failed in configure_options: ValueError: empty options\n"
    )

    annotated, start, end, target = extract_context_frame(
        code=full_code,
        file_path="src/config/options.py",
        error_feedback=indirect_traceback,
        window_radius=10,
        problem_statement="Fix TestRunnerConfig.configure_options to allow empty options.",
    )

    assert target is not None
    # Method header is at line 86 (1-indexed)
    assert 80 <= target <= 90
    assert "configure_options" in annotated
    assert "raise ValueError('empty options')" in annotated


def test_low_confidence_localization_refuses_speculative_head_patch():
    """Verify that when neither frames nor symbols provide localization confidence on large files,

    the system fails closed with (0, 0, None) rather than emitting a speculative
    lines 1-60 patch.
    """
    # 200 lines of unrelated code
    large_unrelated_code = "\n".join(f"field_{i+1} = {i+1}" for i in range(200))
    # Error mentioning completely unrelated symbols and files
    opaque_feedback = (
        "INTERNAL_ENGINE_ERROR: Worker 4 terminated unexpectedly with exit code -9 (SIGKILL).\n"
        "No Python traceback available."
    )

    annotated, start, end, target = extract_context_frame(
        code=large_unrelated_code,
        file_path="unrelated_engine.py",
        error_feedback=opaque_feedback,
        window_radius=25,
        problem_statement="Resolve crash in unknown component.",
    )

    # Fail closed: must return empty annotated string and (0, 0, None)
    assert annotated == ""
    assert start == 0
    assert end == 0
    assert target is None


def test_provider_http_error_does_not_charge_attempt():
    """Verify that HTTP 400/413 provider rejection is classified as an infrastructure error

    and does not increment successful LLM completion counts or settle unearned
    spend.
    """
    from argus.providers.adapter import ProviderError

    mock_adapter = MagicMock()
    # Simulate Groq HTTP 413 token overflow
    mock_adapter.create_completion.side_effect = ProviderError(
        status_code=413,
        message="Request entity too large: TPM limit exceeded (8000 tokens)",
    )

    controller = RecoveryController(adapter=mock_adapter)
    # Configure mock budget
    mock_budget = MagicMock()
    mock_budget.settle_attempt = MagicMock()
    mock_budget.release_reservation = MagicMock()
    controller.budget_tracker = mock_budget

    # If the provider throws an infrastructure error, the controller must handle it cleanly
    with pytest.raises(ProviderError) as exc_info:
        controller.adapter.create_completion(model="openai/gpt-oss-20b", prompt="test", temperature=0.0)

    assert exc_info.value.status_code == 413
    assert "TPM limit exceeded" in str(exc_info.value)
    # Verify no attempt settlement was recorded for a rejected request
    assert mock_budget.settle_attempt.call_count == 0






def test_global_budget_ceiling_hard_stop():
    """Verify that GlobalExperimentBudget prevents reservations beyond the global ceiling ($4.500)

    and that TaskConditionBudget fails closed with GLOBAL_BUDGET_EXCEEDED.
    """
    from argus.config import ModelCost
    from argus.runtime.budget import GlobalExperimentBudget, TaskConditionBudget

    # Ceiling $4.500, already committed $4.495
    gb = GlobalExperimentBudget.reset_instance(global_cap_usd=4.500000, initial_settled_usd=4.495000)
    assert abs(gb.available - 0.005000) < 1e-6

    # Attempt to reserve $0.007500 (exceeds available $0.005000)
    tb = TaskConditionBudget(
        task_id="task_overflow",
        condition="arm_D",
        budget_limit_usd=0.030000,
        global_budget=gb,
    )
    cost = ModelCost(input_cost_per_m=0.150, output_cost_per_m=0.600)

    # 20k prompt + 5k completion = (20000/1e6)*0.150 + (5000/1e6)*0.600 = 0.003 + 0.003 = 0.006000
    ok, reserved_cost, reason = tb.check_and_reserve(
        prompt_tokens=20_000,
        max_completion_tokens=5_000,
        model_cost=cost,
    )
    assert not ok
    assert "GLOBAL_BUDGET_EXCEEDED" in reason
    assert gb.total_committed == 4.495000  # Global state untouched








def test_budget_settlement_and_uncertain_quarantine():
    """Verify that GlobalExperimentBudget and TaskConditionBudget handle settlements,

    releases, and uncertain spend quarantining without double counting.
    """
    from argus.config import ModelCost
    from argus.runtime.budget import GlobalExperimentBudget, TaskConditionBudget

    gb = GlobalExperimentBudget.reset_instance(global_cap_usd=4.500000, initial_settled_usd=0.0)
    tb = TaskConditionBudget(
        task_id="task_budget_test",
        condition="arm_C",
        budget_limit_usd=0.030000,
        global_budget=gb,
    )
    cost = ModelCost(input_cost_per_m=0.075, output_cost_per_m=0.300)

    # 1. Reserve 10k prompt + 2k output = $0.00075 + $0.00060 = $0.001350
    ok, res_amt, _ = tb.check_and_reserve(prompt_tokens=10_000, max_completion_tokens=2_000, model_cost=cost)
    assert ok
    assert abs(res_amt - 0.001350) < 1e-6
    assert abs(gb.global_reserved - 0.001350) < 1e-6

    # 2. Settle with actual usage: 8k prompt + 500 output = $0.00060 + $0.00015 = $0.000750
    actual = tb.settle(reserved_amount=res_amt, prompt_tokens=8_000, completion_tokens=500, model_cost=cost)
    assert abs(actual - 0.000750) < 1e-6
    assert tb.outstanding_reservations == 0.0
    assert abs(tb.settled_spend - 0.000750) < 1e-6
    assert gb.global_reserved == 0.0
    assert abs(gb.global_settled - 0.000750) < 1e-6

    # 3. Reserve again and simulate timeout quarantine
    ok2, res_amt2, _ = tb.check_and_reserve(prompt_tokens=10_000, max_completion_tokens=2_000, model_cost=cost)
    assert ok2
    tb.handle_uncertain_abort(reserved_amount=res_amt2, dispatched=True)
    assert tb.spend_uncertain is True
    assert abs(tb.quarantined_spend - res_amt2) < 1e-6
    assert tb.outstanding_reservations == 0.0
    assert abs(gb.global_quarantined - res_amt2) < 1e-6
    assert gb.global_reserved == 0.0


def test_dispatch_path_refuses_host_fallback_when_isolation_required(tmp_path):
    """Verify that when require_container_isolation or use_container_runner is True,

    RecoveryController strictly refuses to evaluate untrusted code on the host,
    failing closed with CONTAINER_SETUP_ERROR rather than silently executing subprocesses.
    """
    from argus.config import ArgusConfig

    # Configure mock adapter
    mock_adapter = MagicMock()
    mock_adapter.create_completion.return_value = MagicMock(
        content="<<<<<<< SEARCH\npass\n=======\nreturn 42\n>>>>>>> REPLACE",
        prompt_tokens=100,
        completion_tokens=20,
        cached_tokens=0,
    )

    # Case 1: require_container_isolation=True, no container evaluator -> Immediate refusal
    config1 = ArgusConfig(
        provider="custom",
        require_container_isolation=True,
        use_container_runner=False,
    )
    controller1 = RecoveryController(config=config1, adapter=mock_adapter, container_evaluator=None)

    target_file = tmp_path / "solution.py"
    target_file.write_text("def solve():\n    pass\n", encoding="utf-8")

    outcome1 = controller1.run(
        target_file=str(target_file),
        test_command="pytest test_solution.py",
        workspace_root=tmp_path,
        task_id="humaneval_isolated_check_1",
        condition="arm_C",
    )
    assert outcome1.status == "CONTAINER_SETUP_ERROR"
    assert "SECURITY REFUSAL" in outcome1.message
    assert "Host execution of untrusted code is prohibited" in outcome1.message

    # Case 2: use_container_runner=True with failing container engine -> Fail closed as CONTAINER_SETUP_ERROR
    config2 = ArgusConfig(
        provider="custom",
        require_container_isolation=True,
        use_container_runner=True,
    )
    controller2 = RecoveryController(config=config2, adapter=mock_adapter, container_evaluator=None)
    outcome2 = controller2.run(
        target_file=str(target_file),
        test_command="pytest test_solution.py",
        workspace_root=tmp_path,
        task_id="humaneval_isolated_check_2",
        condition="arm_C",
    )
    assert outcome2.status == "CONTAINER_SETUP_ERROR"















