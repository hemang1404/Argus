"""Regression tests for the runtime/config remediation (model dispatch, project policy,
pricing, budget hardening, provider config errors, truncation, per-attempt records)."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import List, Optional

import pytest

from argus.config import (
    CASCADE_LADDERS,
    DEFAULT_BUDGET_USD,
    DEFAULT_MAX_ATTEMPTS,
    ArgusConfig,
    ModelCost,
    UnpricedModelError,
)
from argus.providers.adapter import CompletionResponse, ConnectionTestResult, ProviderAdapter
from argus.providers.models import ProviderConfig
from argus.runtime.budget import GlobalExperimentBudget, TaskConditionBudget
from argus.runtime.candidate import CandidateArtifactStore
from argus.runtime.controller import RecoveryController


class RecordingAdapter(ProviderAdapter):
    """Returns scripted responses; records the model each call was dispatched with."""

    def __init__(self, responses: List[str], finish_reasons: Optional[List[Optional[str]]] = None,
                 response_model: Optional[str] = None) -> None:
        self.responses = responses
        self.finish_reasons = finish_reasons or []
        self.response_model = response_model
        self.models: List[Optional[str]] = []
        self.messages: List[list] = []

    def chat_completion(self, messages, temperature=None, max_tokens=None, effort=None, model=None, **kwargs):
        idx = len(self.models)
        self.models.append(model)
        self.messages.append(messages)
        text = self.responses[idx] if idx < len(self.responses) else "no patch"
        finish = self.finish_reasons[idx] if idx < len(self.finish_reasons) else "stop"
        return CompletionResponse(
            content=text, prompt_tokens=100, completion_tokens=50, duration_ms=1.0,
            model=self.response_model or model or "unknown", finish_reason=finish,
        )

    def test_connection(self) -> ConnectionTestResult:
        return ConnectionTestResult(True, 200, 1.0, "m", "ok")


@pytest.fixture
def repo(tmp_path: Path):
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    src = pkg / "calculator.py"
    src.write_text("def compute_total(a, b):\n    # bug\n    return a + b\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_calculator.py").write_text(
        "from mypkg.calculator import compute_total\n"
        "def test_multiply():\n    assert compute_total(3, 4) == 12\n",
        encoding="utf-8",
    )
    return tmp_path, src


def _run(repo, config, adapter, **kw):
    root, src = repo
    controller = RecoveryController(config=config, adapter=adapter)
    return controller.run(
        target_file=src, test_command="pytest tests/test_calculator.py -q",
        workspace_root=root, **kw,
    )


# --- SEC-01 / SEC-02: configured model is authoritative ------------------------------------

def test_pinned_model_is_dispatched_on_every_attempt(repo):
    adapter = RecordingAdapter(["nothing useful"] * 3)
    cfg = ArgusConfig(provider="openai", model="gpt-4o", max_attempts=3, budget_limit_usd=1.0)
    outcome = _run(repo, cfg, adapter)
    assert adapter.models == ["gpt-4o", "gpt-4o", "gpt-4o"]
    assert {h.model_used for h in outcome.history} == {"gpt-4o"}
    assert any("model_ladder=pinned-model" in r for r in outcome.routing_reasons)


def test_unknown_provider_gets_no_substitution_or_escalation():
    cfg = ArgusConfig(provider="custom", model="my-local-model")
    assert (cfg.model, cfg.small_model, cfg.large_model) == ("my-local-model",) * 3
    assert cfg.model_for_tier(1) == cfg.model_for_tier(3) == "my-local-model"


def test_ollama_model_not_replaced_by_qwen():
    cfg = ArgusConfig(provider="ollama", model="llama3:70b")
    assert cfg.model_for_tier(1) == cfg.model_for_tier(3) == "llama3:70b"


def test_cascade_only_when_model_not_pinned_and_provider_known():
    cfg = ArgusConfig(provider="openai")
    small, large = CASCADE_LADDERS["openai"]
    assert cfg.ladder_source == "provider-cascade"
    assert [cfg.model_for_tier(t) for t in (1, 2, 3)] == [small, small, large]
    # Escalation disabled and nothing pinned: a single provider-default model, no cascade.
    off = ArgusConfig(provider="openai", escalation_enabled=False)
    assert off.ladder_source == "provider-default"
    assert off.model_for_tier(1) == off.model_for_tier(3) == off.model


def test_explicit_ladder_is_honoured_and_visible(repo):
    adapter = RecordingAdapter(["x"] * 3)
    cfg = ArgusConfig(provider="antigravity", small_model="gemini-3.8-flash-low",
                      large_model="gemini-3.8-flash-high", max_attempts=3, budget_limit_usd=1.0)
    outcome = _run(repo, cfg, adapter)
    assert adapter.models == ["gemini-3.8-flash-low", "gemini-3.8-flash-low", "gemini-3.8-flash-high"]
    assert any("model_ladder=explicit-ladder" in r for r in outcome.routing_reasons)


def test_argus_auto_pseudo_model_is_not_sent_upstream():
    cfg = ArgusConfig(provider="ollama", model="argus/auto")
    assert cfg.model != "argus/auto"
    assert cfg.model == cfg.model_for_tier(1)


def test_provider_config_model_does_not_override_explicit_model():
    pc = ProviderConfig(name="openai", base_url="https://api.openai.com/v1", model="gpt-4o-mini", api_key="k")
    cfg = ArgusConfig(model="gpt-4o", provider_config=pc)
    assert cfg.model == "gpt-4o"


def test_executed_model_reported_by_provider_is_recorded(repo):
    adapter = RecordingAdapter(["x"], response_model="gemini-3.8-flash-low")
    cfg = ArgusConfig(provider="antigravity", model="gemini-2.5-flash", max_attempts=1, budget_limit_usd=1.0)
    outcome = _run(repo, cfg, adapter)
    assert outcome.history[0].model_used == "gemini-3.8-flash-low"
    assert any("requested model 'gemini-2.5-flash'" in r for r in outcome.routing_reasons)


def test_antigravity_does_not_downgrade_pro_to_flash(tmp_path):
    from argus.providers.antigravity import AntigravityHostAdapter
    cfg = ProviderConfig(name="antigravity", base_url="host://antigravity-cli", model="gemini-2.5-pro")
    adapter = AntigravityHostAdapter.__new__(AntigravityHostAdapter)
    adapter.config = cfg
    adapter._is_agentapi = False
    adapter.get_cli_path = lambda: Path("agy")
    adapter._get_base_cmd = lambda: ["agy"]
    for name in ("gemini-2.5-pro", "gemini-3-pro", "gemini-3.8-pro", "pro"):
        args, resolved = adapter._build_command("p", "high", name)
        assert resolved == name
        assert args[args.index("--model") + 1] == name


def test_adapter_without_model_parameter_is_refused_not_silently_default(repo):
    class NoModelAdapter(ProviderAdapter):
        def chat_completion(self, messages, temperature=None, max_tokens=None):  # no model, no **kwargs
            raise AssertionError("must not be called")

        def test_connection(self):
            return ConnectionTestResult(True, 200, 1.0, "m", "ok")

    cfg = ArgusConfig(provider="openai", model="gpt-4o", max_attempts=2, budget_limit_usd=1.0)
    outcome = _run(repo, cfg, NoModelAdapter())
    assert outcome.status == "CONFIG_ERROR"
    assert "model override" in outcome.message
    assert outcome.total_cost_usd == 0.0


def test_type_error_inside_adapter_is_not_retried(repo):
    calls = []

    class Flaky(ProviderAdapter):
        def chat_completion(self, messages, model=None, **kwargs):
            calls.append(model)
            raise TypeError("boom after request")

        def test_connection(self):
            return ConnectionTestResult(True, 200, 1.0, "m", "ok")

    cfg = ArgusConfig(provider="openai", model="gpt-4o", max_attempts=1, budget_limit_usd=1.0)
    _run(repo, cfg, Flaky())
    assert calls == ["gpt-4o"]


# --- SEC-04: project policy is tighten-only, read from the workspace root -------------------

def test_project_policy_cannot_raise_caller_limits(tmp_path):
    (tmp_path / "argus.yaml").write_text(
        "budget_limit_usd: 100\nmax_attempts: 9\nlarge_model: gpt-4o\nmodel: gpt-4o\n", encoding="utf-8")
    cfg = ArgusConfig(provider="openai", model="gpt-4o-mini", budget_limit_usd=0.03, max_attempts=3)
    notes = cfg.apply_project_policy(tmp_path)
    assert (cfg.budget_limit_usd, cfg.max_attempts) == (0.03, 3)
    assert cfg.model == "gpt-4o-mini" and cfg.large_model == "gpt-4o-mini"
    assert any("cannot raise" in n for n in notes)
    assert any("cannot select the model" in n for n in notes)


def test_project_policy_may_tighten(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.argus]\nmax_attempts = 2\nbudget_limit_usd = 0.01\nescalation_enabled = false\n", encoding="utf-8")
    cfg = ArgusConfig(provider="ollama", model="m", budget_limit_usd=0.05, max_attempts=3)
    notes = cfg.apply_project_policy(tmp_path)
    assert (cfg.max_attempts, cfg.budget_limit_usd, cfg.escalation_enabled) == (2, 0.01, False)
    assert any("tightened" in n for n in notes)


@pytest.mark.parametrize("bad", ["nan", ".nan", ".inf", "-1", "0", "abc"])
def test_project_policy_invalid_or_nonfinite_values_ignored(tmp_path, bad):
    (tmp_path / "argus.yaml").write_text(f"budget_limit_usd: {bad}\n", encoding="utf-8")
    cfg = ArgusConfig(provider="ollama", model="m", budget_limit_usd=0.05)
    cfg.apply_project_policy(tmp_path)
    assert cfg.budget_limit_usd == 0.05


def test_project_policy_parse_error_is_surfaced(tmp_path):
    (tmp_path / "argus.yaml").write_text("a: [unclosed\n", encoding="utf-8")
    cfg = ArgusConfig(provider="ollama", model="m")
    notes = cfg.apply_project_policy(tmp_path)
    assert any("Could not read project policy argus.yaml" in n for n in notes)


def test_policy_read_from_workspace_root_not_cwd(tmp_path, monkeypatch, repo):
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    (cwd / "argus.yaml").write_text("max_attempts: 1\n", encoding="utf-8")
    monkeypatch.chdir(cwd)
    adapter = RecordingAdapter(["x"] * 3)
    cfg = ArgusConfig(provider="openai", model="gpt-4o", max_attempts=3, budget_limit_usd=1.0)
    outcome = _run(repo, cfg, adapter)
    assert outcome.total_attempts == 3  # the cwd file was not applied
    assert cfg.max_attempts == 3


def test_project_policy_applies_per_run_and_does_not_leak(repo):
    root, _ = repo
    (root / "argus.yaml").write_text("max_attempts: 1\n", encoding="utf-8")
    adapter = RecordingAdapter(["x"] * 3)
    cfg = ArgusConfig(provider="openai", model="gpt-4o", max_attempts=3, budget_limit_usd=1.0)
    outcome = _run(repo, cfg, adapter)
    assert outcome.total_attempts == 1
    assert any("tightened max_attempts" in n for n in outcome.config_notes)
    assert cfg.max_attempts == 3


# --- SEC-07: unpriced models fail closed ----------------------------------------------------

def test_unpriced_model_on_paid_provider_raises():
    cfg = ArgusConfig(provider="openai", model="gpt-5")
    with pytest.raises(UnpricedModelError):
        cfg.get_model_cost()
    assert any("gpt-5" in i for i in cfg.validate())


def test_unpriced_model_with_explicit_price_is_allowed():
    cfg = ArgusConfig(provider="openai", model="gpt-5", model_prices={"gpt-5": ModelCost(1.25, 10.0)})
    assert cfg.get_model_cost().output_cost_per_m == 10.0


def test_local_provider_unpriced_model_is_free():
    assert ArgusConfig(provider="ollama", model="llama3:70b").get_model_cost().input_cost_per_m == 0.0


def test_unpriced_model_gives_config_error_before_any_call(repo):
    adapter = RecordingAdapter(["x"])
    cfg = ArgusConfig(provider="openai", model="gpt-5", budget_limit_usd=1.0)
    outcome = _run(repo, cfg, adapter)
    assert outcome.status == "CONFIG_ERROR" and "gpt-5" in outcome.message
    assert adapter.models == []


# --- SEC-08: non-finite / negative budgets and usage ----------------------------------------

@pytest.mark.parametrize("bad", [math.nan, math.inf, -1.0])
def test_non_finite_budget_limit_rejected(bad):
    with pytest.raises(ValueError):
        TaskConditionBudget(budget_limit_usd=bad)
    assert any("budget_limit_usd" in i for i in ArgusConfig(provider="ollama", model="m", budget_limit_usd=bad).validate())


def test_global_budget_rejects_nan_cap_and_nan_reservation():
    with pytest.raises(ValueError):
        GlobalExperimentBudget(global_cap_usd=math.nan)
    assert GlobalExperimentBudget(global_cap_usd=1.0).reserve(math.nan) is False


def test_negative_usage_is_rejected_not_credited():
    tb = TaskConditionBudget(budget_limit_usd=1.0)
    cost = ModelCost(5.0, 15.0)
    ok, reserved, _ = tb.check_and_reserve(1000, 1000, cost)
    assert ok
    with pytest.raises(ValueError):
        tb.settle(reserved, prompt_tokens=-1_000_000, completion_tokens=0, model_cost=cost)
    assert tb.settled_spend == 0.0
    ok, _, reason = tb.check_and_reserve(-5, 10, cost)
    assert not ok and "INVALID_TOKEN_COUNT" in reason


def test_cached_tokens_cannot_exceed_prompt_tokens_in_cost():
    cost = ModelCost(10.0, 10.0, cached_input_cost_per_m=5.0)
    assert cost.calculate_cost(100, 0, cached_prompt_tokens=10_000) == cost.calculate_cost(100, 0, cached_prompt_tokens=100)


# --- SEC-09: unknown provider -> immediate CONFIG_ERROR -------------------------------------

def test_unknown_provider_is_config_error_with_no_spend(repo):
    root, src = repo
    cfg = ArgusConfig(provider="foo", model="some-model", budget_limit_usd=1.0, model_prices={"some-model": ModelCost(1, 1)})
    outcome = RecoveryController(config=cfg).run(
        target_file=src, test_command="pytest tests/test_calculator.py -q", workspace_root=root)
    assert outcome.status == "CONFIG_ERROR"
    assert "foo" in outcome.message
    assert outcome.total_cost_usd == 0.0
    assert not outcome.spend_uncertain and outcome.quarantined_usd == 0.0


def test_configuration_error_raised_lazily_releases_reservation(repo, monkeypatch):
    from argus.providers.models import ProviderConfigurationError
    root, src = repo
    cfg = ArgusConfig(provider="openai", model="gpt-4o", budget_limit_usd=1.0)
    controller = RecoveryController(config=cfg, adapter=RecordingAdapter([]))

    def boom(*a, **k):
        raise ProviderConfigurationError("No API key configured for provider 'openai'.")

    monkeypatch.setattr(controller, "_call_model", boom)
    outcome = controller.run(target_file=src, test_command="pytest tests/test_calculator.py -q", workspace_root=root)
    assert outcome.status == "CONFIG_ERROR"
    assert outcome.total_cost_usd == 0.0 and outcome.quarantined_usd == 0.0 and not outcome.spend_uncertain


# --- Global singleton must not leak into product runs ---------------------------------------

def test_product_runs_do_not_use_global_experiment_budget(repo):
    exhausted = GlobalExperimentBudget.reset_instance(global_cap_usd=0.0)
    try:
        adapter = RecordingAdapter(["x"])
        cfg = ArgusConfig(provider="openai", model="gpt-4o", max_attempts=1, budget_limit_usd=1.0)
        outcome = _run(repo, cfg, adapter)
        assert outcome.status != "BUDGET_EXCEEDED"
        assert adapter.models == ["gpt-4o"]
        assert exhausted.global_reserved == 0.0 and exhausted.global_settled == 0.0
    finally:
        GlobalExperimentBudget.set_instance(None)


def test_single_source_of_truth_defaults():
    cfg = ArgusConfig(provider="ollama", model="m")
    assert cfg.max_attempts == DEFAULT_MAX_ATTEMPTS
    assert cfg.budget_limit_usd == DEFAULT_BUDGET_USD
    assert TaskConditionBudget().budget_limit_usd == DEFAULT_BUDGET_USD


# --- Truncation + per-attempt records --------------------------------------------------------

def test_truncated_output_gets_truncation_feedback_not_format_error(repo):
    good = "<<<<<<< SEARCH\n    return a + b\n=======\n    return a * b\n>>>>>>> REPLACE"
    adapter = RecordingAdapter(["<<<<<<< SEARCH\n    return a +", good], finish_reasons=["length", "stop"])
    cfg = ArgusConfig(provider="openai", model="gpt-4o", max_attempts=2, budget_limit_usd=1.0)
    outcome = _run(repo, cfg, adapter)
    assert outcome.status == "SUCCESS"
    first = outcome.history[0]
    assert first.finish_reason == "length" and "truncated" in first.error_summary
    second_prompt = adapter.messages[1][1]["content"]
    assert "hit the output token limit" in second_prompt
    assert "Formatting error" not in second_prompt


def test_attempt_records_are_persisted_for_replay(repo):
    root, _ = repo
    adapter = RecordingAdapter(["garbage", "garbage"], finish_reasons=["stop", "length"])
    cfg = ArgusConfig(provider="openai", model="gpt-4o", max_attempts=2, budget_limit_usd=1.0)
    outcome = _run(repo, cfg, adapter)
    run_dir = CandidateArtifactStore.get_runs_dir(root) / outcome.run_id
    records = json.loads((run_dir / "attempts.json").read_text(encoding="utf-8"))
    assert [r["attempt"] for r in records] == [1, 2]
    assert [r["finish_reason"] for r in records] == ["stop", "length"]
    assert all(r["model_used"] == "gpt-4o" and r["prompt_tokens"] == 100 for r in records)
    assert all(r["patch_success"] is False and r["test_passed"] is False for r in records)
