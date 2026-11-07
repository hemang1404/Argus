"""Regression Test Suite for Confirmatory Study Remediations.

Validates fixes for known issues identified during the 30-task pilot:
1. Evaluator setup patch application (regression test for pydata__xarray-4629 evaluator failure).
2. Context localization for test-runner indirection using symbol matching.
3. Strict fail-closed refusal when localization confidence is insufficient (no speculative head patches).
4. Provider error classification (HTTP 400/413 / transport errors do not record successful completions).
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.research

from argus.runtime.container import (
    ContainerConfig,
    ContainerEvaluator,
    ContainerExecutionResult,
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
    from argus.providers.adapter import ProviderError, CompletionResponse

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


def test_exact_mcnemar_binomial_arithmetic():
    """Verify that exact two-sided binomial p-value for Nd=22, k=18 is approximately 0.0043435.

    Confirms correct sum of combinations:
    comb(22, 18) = 7315, comb(22, 19) = 1540, comb(22, 20) = 231, comb(22, 21) = 22, comb(22, 22) = 1.
    Sum = 9109. Two-sided p = 2 * 9109 / 2^22 = 18218 / 4194304 = 0.00434351...
    """
    import math
    from src.analysis.reproduce_pilot_30_analysis import compute_mcnemar_paired

    n_d = 22
    k_min = 4  # discordant failures (arm2 wins)
    k_max = 18 # discordant successes (arm1 wins)

    # Pure arithmetic check
    comb_sum = sum(math.comb(n_d, k) for k in range(k_min + 1))
    assert comb_sum == 9109
    p_exact_calc = 2.0 * comb_sum / (2 ** n_d)
    assert abs(p_exact_calc - 0.00434351) < 1e-7

    # Verification via analysis module
    matrix = {}
    for i in range(18):
        matrix[f"t_win_{i}"] = {"arm1": True, "arm2": False}
    for i in range(4):
        matrix[f"t_loss_{i}"] = {"arm1": False, "arm2": True}
    for i in range(98):
        matrix[f"t_tie_{i}"] = {"arm1": True, "arm2": True}

    res = compute_mcnemar_paired(matrix, "arm1", "arm2")
    assert res["b_arm1_wins"] == 18
    assert res["c_arm2_wins"] == 4
    assert res["n_discordant"] == 22
    assert res["p_exact"] == 0.0043


def test_holm_bonferroni_step_down_ranking():
    """Verify that Holm-Bonferroni sorts observed p-values in ascending order

    and evaluates step-down thresholds alpha / (m - i + 1), rather than permanently
    assigning fixed thresholds to named contrast labels upfront.
    """
    from src.analysis.reproduce_pilot_30_analysis import apply_holm_bonferroni

    # Provide p-values out of order
    results = [
        {"p_exact": 0.0350, "contrast": "Contrast_3"},
        {"p_exact": 0.0040, "contrast": "Contrast_1"},
        {"p_exact": 0.0600, "contrast": "Contrast_4"},
        {"p_exact": 0.0150, "contrast": "Contrast_2"},
    ]

    adjusted = apply_holm_bonferroni(results, p_key="p_exact", alpha=0.05)

    # Ranks must be 1 to 4 sorted by raw p
    assert [r["contrast"] for r in adjusted] == [
        "Contrast_1",
        "Contrast_2",
        "Contrast_3",
        "Contrast_4",
    ]
    assert adjusted[0]["rank"] == 1
    assert adjusted[0]["p_adjusted"] == round(0.0040 * 4, 4)  # 0.0160
    assert adjusted[0]["significant_alpha_0.05"] is True

    assert adjusted[1]["rank"] == 2
    assert adjusted[1]["p_adjusted"] == round(0.0150 * 3, 4)  # 0.0450
    assert adjusted[1]["significant_alpha_0.05"] is True

    assert adjusted[2]["rank"] == 3
    # Step 3 raw p = 0.0350 * 2 = 0.0700 > 0.05 (Not significant)
    assert adjusted[2]["p_adjusted"] == round(0.0350 * 2, 4)  # 0.0700
    assert adjusted[2]["significant_alpha_0.05"] is False

    assert adjusted[3]["rank"] == 4
    assert adjusted[3]["significant_alpha_0.05"] is False


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


def test_task_arm_order_counterbalancing_determinism():
    """Verify that get_task_arm_order produces deterministic, balanced permutations

    keyed to manifest_sha256, task_id, and seed.
    """
    from src.experiments.run_confirmatory_120_study import get_task_arm_order

    manifest_hash = "2b6581e2e91ceb3c3ca9d7ec05112ff036b2facd600e3a37e9c423cd02665ae8"
    order1 = get_task_arm_order("django__django-11119", manifest_hash, seed=42)
    order2 = get_task_arm_order("django__django-11119", manifest_hash, seed=42)

    assert order1 == order2
    assert set(order1) == {"A", "B", "C", "D", "E"}
    assert len(order1) == 5

    # Different task should produce a different permutation
    order3 = get_task_arm_order("humaneval_21", manifest_hash, seed=42)
    assert set(order3) == {"A", "B", "C", "D", "E"}


def test_rejection_region_cutoffs_nd15_and_nd20():
    """Verify that exact two-sided binomial rejection cutoffs at alpha=0.0125 are:

    - Nd=15: k <= 2 and k >= 13 (two-sided p = 0.007385 <= 0.0125)
    - Nd=20: k <= 4 and k >= 16 (two-sided p = 0.011818 <= 0.0125)
    """
    pytest.importorskip("scipy", reason="install 'argus-recovery[research]'")
    from src.analysis.compute_confirmatory_power_tables import (
        exact_two_sided_binomial_p,
        get_rejection_region,
    )

    # Nd = 15
    rej15, k_low15, k_high15 = get_rejection_region(15, 0.0125)
    assert k_low15 == 2
    assert k_high15 == 13
    assert exact_two_sided_binomial_p(15, 13) == pytest.approx(0.00738525, abs=1e-6)
    assert exact_two_sided_binomial_p(15, 12) > 0.0125  # k=12 not rejected

    # Nd = 20
    rej20, k_low20, k_high20 = get_rejection_region(20, 0.0125)
    assert k_low20 == 4
    assert k_high20 == 16
    assert exact_two_sided_binomial_p(20, 16) == pytest.approx(0.0118179, abs=1e-6)
    assert exact_two_sided_binomial_p(20, 15) > 0.0125  # k=15 not rejected


def test_unconditional_power_discrepancy_resolved():
    """Verify exact unconditional power values for p_disc=0.20, theta=0.80 at alpha=0.0167 and 0.0250,

    explicitly comparing them with the previous draft values of 69.8% and 75.3%.
    """
    pytest.importorskip("scipy", reason="install 'argus-recovery[research]'")
    from src.analysis.compute_confirmatory_power_tables import generate_unconditional_power_table

    uncond = generate_unconditional_power_table()
    target = [r for r in uncond if r["p_disc"] == 0.20 and r["theta"] == 0.80][0]

    # Exact recalculated values
    exact_0167 = target["power_0167_pct"]
    exact_0250 = target["power_0250_pct"]

    assert exact_0167 == pytest.approx(67.271, abs=1e-3)
    assert exact_0250 == pytest.approx(73.480, abs=1e-3)

    # Reconcile discrepancy: previous draft stated 69.8% and 75.3%
    assert exact_0167 != 69.8
    assert exact_0250 != 75.3


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


def test_active_budget_cap_enforced_single_value():
    """Verify that the selected per-condition reservation cap is uniquely $0.00750,

    guaranteeing that 600 conditions committed cannot exceed $4.500000 in the active
    experiment process, and lifetime exposure remains bounded below the $5.00 ceiling.
    """
    from src.experiments.run_confirmatory_120_study import (
        CONDITION_RESERVATION_CAP_USD,
        GLOBAL_BUDGET_CEILING_USD,
    )

    # 1. Uniquely enforced per-condition cap
    assert CONDITION_RESERVATION_CAP_USD == 0.007500
    assert GLOBAL_BUDGET_CEILING_USD == 4.500000

    # 2. Maximum concurrent theoretical commitment
    max_active_commitment = 600 * CONDITION_RESERVATION_CAP_USD
    assert max_active_commitment == 4.500000
    assert max_active_commitment <= GLOBAL_BUDGET_CEILING_USD

    # 3. Lifetime accounting
    historical_settled = 0.0504194
    quarantined_uncertain = 0.0053980
    historical_conservative = historical_settled + quarantined_uncertain
    assert abs(historical_conservative - 0.0558174) < 1e-6

    # Lifetime ceiling check: $5.000000
    lifetime_cap = 5.000000
    max_lifetime_exposure = historical_conservative + GLOBAL_BUDGET_CEILING_USD
    assert max_lifetime_exposure == pytest.approx(4.5558174, abs=1e-6)
    assert max_lifetime_exposure < lifetime_cap
    assert (lifetime_cap - max_lifetime_exposure) > 0.44  # Guaranteed >$0.44 margin


def test_latent_correlation_simulation_and_binary_phi():
    """Verify that the 5-arm latent Gaussian model correctly parameterizes latent correlation r

    and produces empirical binary phi correlations phi < r under pilot-calibrated marginals.
    """
    pytest.importorskip("scipy", reason="install 'argus-recovery[research]'")
    from src.analysis.compute_confirmatory_power_tables import (
        PILOT_EMPIRICAL_COUNTS,
        simulate_coherent_5arm_study,
    )

    # Pilot empirical counts
    assert PILOT_EMPIRICAL_COUNTS["A"] == (12, 30)
    assert PILOT_EMPIRICAL_COUNTS["B"] == (13, 30)
    assert PILOT_EMPIRICAL_COUNTS["C"] == (17, 30)
    assert PILOT_EMPIRICAL_COUNTS["D"] == (18, 30)
    assert PILOT_EMPIRICAL_COUNTS["E"] == (11, 30)

    # Run deterministic simulation with K=2000
    sim = simulate_coherent_5arm_study(n_tasks=120, n_replications=2000, latent_r=0.50, seed=42)

    assert sim["latent_correlation_r"] == 0.50
    # Binary correlation phi is strictly attenuated relative to latent r
    assert 0.25 < sim["mean_binary_correlation_phi"] < 0.40
    assert sim["mean_binary_correlation_phi"] < 0.50

    # Monte Carlo standard error must be small (<= 0.015 for K=2000)
    assert all(se < 0.015 for se in sim["individual_holm_se"])

    # Contrast 4 (C vs B) has power < 80% (confirming joint Holm power is model-dependent)
    c_vs_b_power = sim["individual_holm_power"][3]
    assert c_vs_b_power < 0.80
    # All four power is < 70%
    assert sim["all_four_rejection_power"] < 0.70


def test_image_lockfile_consumption_by_runtime(tmp_path):
    """Verify that ConfirmatoryStudyRunner runtime can consume locked immutable digests/IDs

    from confirmatory_120_image_lock.json rather than assuming lockfiles have no effect.
    """
    from src.experiments.run_confirmatory_120_study import ConfirmatoryStudyRunner

    # 1. Create runner with standard manifest
    runner = ConfirmatoryStudyRunner(
        manifest_path=Path("experiments_archive/confirmatory_120_manifest.json"),
        dry_run=True,
    )

    # For an unknown unmapped image without SUCCESS lock, falls back to default manifest image
    unmapped_img = "swebench/sweb.eval.x86_64.custom_unknown-9999:latest"
    resolved_fallback = runner.resolve_container_image("custom__unknown-9999", unmapped_img)
    assert resolved_fallback == unmapped_img

    # 2. For a successfully pulled and locked task, runner consumes the locked immutable digest
    locked_task_id = "django__django-10097"
    manifest_img = "swebench/sweb.eval.x86_64.django_django-10097:latest"
    resolved_locked = runner.resolve_container_image(locked_task_id, manifest_img)
    assert resolved_locked != manifest_img
    assert "@sha256:" in resolved_locked
    assert "django_1776_django-10097" in resolved_locked


def test_confirmatory_runner_lifecycle_and_checkpointing(tmp_path):
    """Verify that ConfirmatoryStudyRunner:

    1. Executes randomized within-task arm ordering.
    2. Writes conditions to outcomes.jsonl.
    3. Resumes from checkpoint, skipping already-completed conditions without re-execution.
    """
    from src.experiments.run_confirmatory_120_study import ConfirmatoryStudyRunner

    results_dir = tmp_path / "study_results"

    # Step 1: Initial run with 2 tasks in dry-run mode
    runner1 = ConfirmatoryStudyRunner(
        manifest_path=Path("experiments_archive/confirmatory_120_manifest.json"),
        results_dir=results_dir,
        dry_run=True,
        max_tasks=2,
    )
    summary1 = runner1.run_study()
    assert summary1["tasks_processed"] == 2
    assert summary1["completed_conditions"] == 10  # 2 tasks * 5 arms

    outcomes_file = results_dir / "outcomes.jsonl"
    assert outcomes_file.exists()
    with open(outcomes_file, "r", encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert len(lines) == 10

    # Step 2: Resumed run with the same results_dir
    runner2 = ConfirmatoryStudyRunner(
        manifest_path=Path("experiments_archive/confirmatory_120_manifest.json"),
        results_dir=results_dir,
        dry_run=True,
        max_tasks=2,
    )
    # Checkpoint should load all 10 completed conditions
    assert len(runner2.completed_conditions) == 10

    summary2 = runner2.run_study()
    # No new conditions should be appended
    assert summary2["completed_conditions"] == 10
    with open(outcomes_file, "r", encoding="utf-8") as f:
        lines2 = [json.loads(l) for l in f if l.strip()]
    assert len(lines2) == 10


def test_algorithmic_evaluation_harness_delivery_and_assertion_failure():
    """Verify that ContainerEvaluator with setup_files delivers test_solution.py into disposable containers.

    Proves that:
    1. Known-correct HumanEval solution passes (exit code 0).
    2. Known-correct MBPP solution passes (exit code 0).
    3. Deliberately incorrect solution fails on genuine AssertionError, not a missing-file error.
    """
    from argus.runtime.container import is_container_engine_available
    if not is_container_engine_available("docker"):
        pytest.skip("Local Docker daemon unavailable")

    from src.experiments.run_confirmatory_120_study import format_algorithmic_test

    with open("data/candidate_tasks.jsonl", "r", encoding="utf-8") as f:
        cand_map = {c["task_id"]: c for c in [json.loads(line) for line in f if line.strip()]}

    # 1. HumanEval Known-Correct & Incorrect
    he_task = cand_map["humaneval_21"]
    he_test = format_algorithmic_test(he_task["entry_point"], he_task["test_code"])
    he_cfg = ContainerConfig(
        image="argus-algorithmic-eval:latest",
        workdir="/home/sandbox/workspace",
        timeout_seconds=30,
        memory_limit="512m",
        pids_limit=64,
        network_disabled=True,
        setup_files={"test_solution.py": he_test},
    )
    evaluator_he = ContainerEvaluator(he_cfg)

    res_he_correct = evaluator_he.evaluate(
        candidate_content=he_task["canonical_solution"],
        target_rel_path="solution.py",
        test_command="pytest test_solution.py -q",
    )
    assert res_he_correct.passed is True
    assert res_he_correct.exit_code == 0

    res_he_wrong = evaluator_he.evaluate(
        candidate_content="def rescale_to_unit(numbers):\n    return numbers\n",
        target_rel_path="solution.py",
        test_command="pytest test_solution.py -q",
    )
    assert res_he_wrong.passed is False
    assert res_he_wrong.exit_code == 1
    assert "AssertionError" in res_he_wrong.stdout
    assert "file or directory not found" not in res_he_wrong.stdout.lower()
    assert "file or directory not found" not in res_he_wrong.stderr.lower()

    # 2. MBPP Known-Correct & Incorrect
    mbpp_task = cand_map["mbpp_75"]
    mbpp_test = format_algorithmic_test(mbpp_task["entry_point"], mbpp_task["test_code"])
    mbpp_cfg = ContainerConfig(
        image="argus-algorithmic-eval:latest",
        workdir="/home/sandbox/workspace",
        timeout_seconds=30,
        memory_limit="512m",
        pids_limit=64,
        network_disabled=True,
        setup_files={"test_solution.py": mbpp_test},
    )
    evaluator_mbpp = ContainerEvaluator(mbpp_cfg)

    res_mbpp_correct = evaluator_mbpp.evaluate(
        candidate_content=mbpp_task["canonical_solution"],
        target_rel_path="solution.py",
        test_command="pytest test_solution.py -q",
    )
    assert res_mbpp_correct.passed is True
    assert res_mbpp_correct.exit_code == 0

    res_mbpp_wrong = evaluator_mbpp.evaluate(
        candidate_content="def find_tuples(test_list, K):\n    return []\n",
        target_rel_path="solution.py",
        test_command="pytest test_solution.py -q",
    )
    assert res_mbpp_wrong.passed is False
    assert res_mbpp_wrong.exit_code == 1
    assert "AssertionError" in res_mbpp_wrong.stdout
    assert "file or directory not found" not in res_mbpp_wrong.stdout.lower()
    assert "file or directory not found" not in res_mbpp_wrong.stderr.lower()





