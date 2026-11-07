"""Milestone 1 Regression Suite: Provider Configuration, Official Pricing,
Pre-Dispatch Budget Reservation, Uncertain Spend Quarantine, and 429 Separation.

All tests run strictly offline with no live paid API calls or SWE-bench benchmarks.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from argus.config import ArgusConfig, MODEL_REGISTRY, ModelCost
from argus.providers.adapter import (
    CompletionResponse,
    OpenAICompatibleAdapter,
    ProviderAdapter,
    ProviderError,
)
from argus.providers.models import PROVIDER_PRESETS, ProviderConfig
from argus.runtime.budget import (
    GlobalExperimentBudget,
    TaskConditionBudget,
    count_prompt_tokens,
)
from argus.runtime.controller import RecoveryController, RecoveryOutcome

pytestmark = pytest.mark.research


# =============================================================================
# 1. Provider Configuration and Official Groq Pricing
# =============================================================================

def test_provider_presets_prohibit_groq_and_configure_openai():
    """Groq preset is strictly prohibited; openai preset points to official endpoint."""
    assert "groq" not in PROVIDER_PRESETS
    preset = PROVIDER_PRESETS["openai"]
    assert preset.key == "openai"
    assert preset.default_base_url == "https://api.openai.com/v1"
    assert preset.requires_key is True


def test_official_groq_model_pricing_math():
    """Verify official Oct 28, 2026 Groq rates with division by 1,000,000.0."""
    # 1. openai/gpt-oss-20b: $0.075 / 1M input, $0.0375 cached, $0.300 / 1M output
    cost_20b = MODEL_REGISTRY["openai/gpt-oss-20b"]
    assert cost_20b.input_cost_per_m == 0.075
    assert cost_20b.cached_input_cost_per_m == 0.0375
    assert cost_20b.output_cost_per_m == 0.300

    # 100,000 input tokens uncached = 0.1 * 0.075 = $0.0075
    # 20,000 output tokens = 0.02 * 0.300 = $0.0060
    # Total = $0.0135
    calc_20b = cost_20b.calculate_cost(prompt_tokens=100_000, completion_tokens=20_000)
    assert abs(calc_20b - 0.0135) < 1e-7

    # With 50,000 cached prompt tokens:
    # 50,000 uncached input = 0.05 * 0.075 = $0.00375
    # 50,000 cached input = 0.05 * 0.0375 = $0.001875
    # 20,000 output = $0.0060
    # Total = $0.011625
    calc_20b_cached = cost_20b.calculate_cost(
        prompt_tokens=100_000,
        completion_tokens=20_000,
        cached_prompt_tokens=50_000,
    )
    assert abs(calc_20b_cached - 0.011625) < 1e-7

    # 2. openai/gpt-oss-120b: $0.150 / 1M input, $0.075 cached, $0.600 / 1M output
    cost_120b = MODEL_REGISTRY["openai/gpt-oss-120b"]
    assert cost_120b.input_cost_per_m == 0.150
    assert cost_120b.cached_input_cost_per_m == 0.075
    assert cost_120b.output_cost_per_m == 0.600

    calc_120b = cost_120b.calculate_cost(prompt_tokens=100_000, completion_tokens=20_000)
    assert abs(calc_120b - 0.0270) < 1e-7


def test_official_gemini_model_pricing_math():
    """Verify standard official Google AI Studio Gemini 2.5 rates (Nov 7, 2026).
    
    Source: https://ai.google.dev/gemini-api/docs/pricing
    """
    # 1. gemini-2.5-flash (prompts <= 200k): $0.30/1M input, $2.50/1M output, $0.075/1M cached
    cost_flash = MODEL_REGISTRY["gemini-2.5-flash"]
    assert cost_flash.input_cost_per_m == 0.300
    assert cost_flash.output_cost_per_m == 2.500
    assert cost_flash.cached_input_cost_per_m == 0.075

    # 100k input, 20k output: (0.10 * 0.30) + (0.02 * 2.50) = 0.030 + 0.050 = $0.080
    calc_flash = cost_flash.calculate_cost(prompt_tokens=100_000, completion_tokens=20_000)
    assert abs(calc_flash - 0.080) < 1e-7

    # 50k cached + 50k uncached input + 20k output:
    # 50k uncached = 0.05 * 0.30 = $0.0150
    # 50k cached = 0.05 * 0.075 = $0.00375
    # 20k output = 0.02 * 2.50 = $0.0500
    # Total = $0.06875
    calc_flash_cached = cost_flash.calculate_cost(
        prompt_tokens=100_000,
        completion_tokens=20_000,
        cached_prompt_tokens=50_000,
    )
    assert abs(calc_flash_cached - 0.06875) < 1e-7

    # 2. gemini-2.5-pro (prompts <= 200k): $1.25/1M input, $10.00/1M output, $0.3125/1M cached
    cost_pro = MODEL_REGISTRY["gemini-2.5-pro"]
    assert cost_pro.input_cost_per_m == 1.250
    assert cost_pro.output_cost_per_m == 10.000
    assert cost_pro.cached_input_cost_per_m == 0.3125

    # 100k input, 20k output (including thinking tokens):
    # (0.10 * 1.25) + (0.02 * 10.00) = 0.125 + 0.200 = $0.325
    calc_pro = cost_pro.calculate_cost(prompt_tokens=100_000, completion_tokens=20_000)
    assert abs(calc_pro - 0.325) < 1e-7

    # 3. gemini-2.5-flash-lite: $0.075/1M input, $0.30/1M output, $0.01875/1M cached
    cost_lite = MODEL_REGISTRY["gemini-2.5-flash-lite"]
    assert cost_lite.input_cost_per_m == 0.075
    assert cost_lite.output_cost_per_m == 0.300
    assert cost_lite.cached_input_cost_per_m == 0.01875

    # 4. Host native models are 0.0 (subscription-authenticated)
    assert MODEL_REGISTRY["antigravity"].input_cost_per_m == 0.0
    assert MODEL_REGISTRY["gemini-3.8-flash-low"].input_cost_per_m == 0.0


def test_gpt_oss_tokenizer_o200k_harmony_selected():
    """Verify that GPT-OSS models resolve to o200k_harmony vocabulary."""
    pytest.importorskip("tiktoken", reason="install 'argus-recovery[research]'")
    from argus.runtime.budget import get_gpt_oss_tokenizer
    tokenizer = get_gpt_oss_tokenizer("openai/gpt-oss-20b")
    assert tokenizer is not None
    assert tokenizer.name == "o200k_harmony"


def test_conservative_prompt_token_counting():
    """GPT-OSS o200k_harmony token estimation accounts for system prompt and message framing."""
    code_sample = "def add(a: int, b: int) -> int:\n    return a + b\n"
    tokens = count_prompt_tokens(code_sample, "openai/gpt-oss-20b")
    # Must account for ARGUS_SYSTEM_PROMPT (~59 tokens) + framing (11) + code + safety buffer
    assert tokens > 75

    # Empty string prompt still accounts for system prompt + wire framing + assistant priming + safety buffer
    empty_tokens = count_prompt_tokens("", "openai/gpt-oss-20b")
    assert empty_tokens >= 80


def test_token_estimation_representative_code_unicode_and_metadata():
    """Verify token estimation across representative code, whitespace, long identifiers, and Unicode."""
    # 1. Whitespace, tabs, and indentation
    code_whitespace = (
        "class DataProcessor:\n"
        "\tdef __init__(self):\n"
        "        self.data = []\n\n\n"
        "    def process(self):\n"
        "        pass\n"
    )
    tokens_ws = count_prompt_tokens(code_whitespace, "openai/gpt-oss-20b")
    assert tokens_ws > 85

    # 2. Long identifiers and unusual string literals
    code_long_id = (
        "super_califragilistic_expialidocious_variable_identifier_v12345 = "
        "r'path/to/directory\\with\\backslashes\\and\\quotes\"\'\\n\\t'\n"
    )
    tokens_id = count_prompt_tokens(code_long_id, "openai/gpt-oss-20b")
    assert tokens_id > 85

    # 3. Unicode, multi-byte characters, and emoji
    code_unicode = (
        "# 日本語テストコメント\n"
        "const_π = 3.141592653589793\n"
        "status = '🚀 Rocket deployed successfully! ✨'\n"
        "formula = '∀x ∈ S: f(x) ≥ 0 ∑_i x_i'\n"
    )
    tokens_unicode = count_prompt_tokens(code_unicode, "openai/gpt-oss-20b")
    assert tokens_unicode > 90

    # 4. Structured message metadata
    custom_messages = [
        {"role": "system", "content": "You are a concise assistant."},
        {"role": "user", "content": "Fix the bug in main.py line 42."},
    ]
    tokens_meta = count_prompt_tokens(custom_messages, "openai/gpt-oss-20b")
    assert tokens_meta >= 30


def test_token_estimation_comparison_against_mocked_provider_telemetry():
    """Verify local conservative estimate is always >= raw wire tokens reported by provider."""
    tiktoken = pytest.importorskip("tiktoken", reason="install 'argus-recovery[research]'")
    enc = tiktoken.get_encoding("o200k_harmony")

    user_prompt = "def calculate_mean(values: list[float]) -> float:\n    return sum(values) / len(values)\n"
    from argus.runtime.budget import ARGUS_SYSTEM_PROMPT

    # Raw wire token count without safety buffer:
    # 4 tokens per message (2 messages = 8) + 3 tokens assistant priming = 11
    # + len(enc.encode(ARGUS_SYSTEM_PROMPT)) + len(enc.encode(user_prompt))
    raw_wire_tokens = (
        11
        + len(enc.encode("system"))
        + len(enc.encode(ARGUS_SYSTEM_PROMPT))
        + len(enc.encode("user"))
        + len(enc.encode(user_prompt))
    )

    estimated_tokens = count_prompt_tokens(user_prompt, "openai/gpt-oss-20b")

    # Local pre-dispatch estimate must be conservative (strictly >= raw wire tokens)
    assert estimated_tokens >= raw_wire_tokens


# =============================================================================
# 2. Pre-Dispatch Budget Refusal
# =============================================================================

def test_pre_dispatch_budget_refusal_before_network_call(tmp_path: Path):
    """When task budget cannot cover the worst-case reservation, fail closed without dispatching."""
    src_file = tmp_path / "mod.py"
    src_file.write_text("def solve():\n    return 0\n", encoding="utf-8")
    test_file = tmp_path / "test_mod.py"
    test_file.write_text(
        "from mod import solve\ndef test_solve():\n    assert solve() == 42\n",
        encoding="utf-8",
    )

    # Set tiny budget ($0.000001) that cannot even cover 1 prompt + completion token
    cfg = ArgusConfig(
        provider="custom",
        budget_limit_usd=0.000001,
        small_model="openai/gpt-oss-20b",
        large_model="openai/gpt-oss-120b",
    )
    controller = RecoveryController(config=cfg)

    # Mock _call_model to verify it is NEVER called
    mock_dispatch = MagicMock(side_effect=AssertionError("Network dispatch must not occur!"))
    controller._call_model = mock_dispatch

    outcome = controller.run(
        target_file=src_file,
        test_command=f"pytest {test_file.name}",
        workspace_root=tmp_path,
        mock_test=True,
    )

    assert outcome.status == "BUDGET_EXCEEDED"
    assert "Pre-dispatch budget refusal" in outcome.message
    assert outcome.total_attempts == 0
    assert mock_dispatch.call_count == 0


# =============================================================================
# 3. Global Experiment Budget Ceiling Enforcement
# =============================================================================

def test_global_experiment_budget_ceiling_enforcement(tmp_path: Path):
    """Global ceiling ($5.00 limit) halts further task execution across runs."""
    src_file = tmp_path / "algo.py"
    src_file.write_text("def run(): return 1\n", encoding="utf-8")
    test_file = tmp_path / "test_algo.py"
    test_file.write_text("from algo import run\ndef test_run(): assert run() == 2\n", encoding="utf-8")

    # Shared global budget with very small ceiling
    global_cap = 0.0010
    global_budget = GlobalExperimentBudget(global_cap_usd=global_cap)
    global_budget.reset()

    # Pre-exhaust global budget
    assert global_budget.reserve(0.00095)
    global_budget.settle(0.00095, 0.00095)
    assert global_budget.available < 0.0001

    cfg = ArgusConfig(
        provider="custom",
        model="openai/gpt-oss-20b",
        budget_limit_usd=0.030,
        global_experiment_budget_usd=global_cap,
    )
    controller = RecoveryController(config=cfg)

    mock_dispatch = MagicMock(side_effect=AssertionError("Must not dispatch when global budget exhausted"))
    controller._call_model = mock_dispatch

    outcome = controller.run(
        target_file=src_file,
        test_command=f"pytest {test_file.name}",
        workspace_root=tmp_path,
        global_budget=global_budget,
        mock_test=True,
    )

    assert outcome.status == "BUDGET_EXCEEDED"
    assert "GLOBAL_BUDGET_EXCEEDED" in outcome.message
    assert mock_dispatch.call_count == 0


# =============================================================================
# 4. Post-Dispatch Timeout and Uncertain Spend Quarantine
# =============================================================================

def test_post_dispatch_timeout_quarantines_uncertain_spend(tmp_path: Path):
    """When a timeout occurs after socket dispatch, spend must be quarantined and marked uncertain."""
    src_file = tmp_path / "srv.py"
    src_file.write_text("def ping(): return False\n", encoding="utf-8")
    test_file = tmp_path / "test_srv.py"
    test_file.write_text("from srv import ping\ndef test_ping(): assert ping() is True\n", encoding="utf-8")

    global_budget = GlobalExperimentBudget(global_cap_usd=5.0)
    global_budget.reset()

    cfg = ArgusConfig(
        provider="custom",
        model="openai/gpt-oss-20b",
        budget_limit_usd=0.030,
        max_attempts=1,
    )
    controller = RecoveryController(config=cfg)

    # Simulate timeout post-dispatch (HTTP 504 / read timeout)
    def raise_timeout(*args, **kwargs):
        raise ProviderError("Request to groq timed out after 45.0s", status_code=504)

    controller._call_model = raise_timeout

    outcome = controller.run(
        target_file=src_file,
        test_command=f"pytest {test_file.name}",
        workspace_root=tmp_path,
        global_budget=global_budget,
        mock_test=True,
    )

    assert outcome.status == "FAILED_MAX_ATTEMPTS"
    assert outcome.spend_uncertain is True
    assert outcome.quarantined_usd > 0.0
    assert outcome.total_cost_usd >= outcome.quarantined_usd
    assert abs(global_budget.global_quarantined - outcome.quarantined_usd) < 1e-5
    assert outcome.total_attempts == 1
    assert "quarantined" in outcome.history[0].error_summary


# =============================================================================
# 5. HTTP 429 Rate Limit Backoff & Separation from Repair Attempts
# =============================================================================

def test_rate_limit_429_does_not_consume_repair_attempts_or_spend(tmp_path: Path):
    """HTTP 429 rate limit backoff retries without incrementing attempts or consuming budget."""
    src_file = tmp_path / "math_fn.py"
    src_file.write_text("def compute():\n    return 10\n", encoding="utf-8")
    test_file = tmp_path / "test_math_fn.py"
    test_file.write_text("from math_fn import compute\ndef test_compute():\n    assert compute() == 20\n", encoding="utf-8")

    global_budget = GlobalExperimentBudget(global_cap_usd=5.0)
    global_budget.reset()

    cfg = ArgusConfig(
        provider="custom",
        model="openai/gpt-oss-20b",
        budget_limit_usd=0.030,
        max_attempts=2,
    )
    controller = RecoveryController(config=cfg)

    # First call: 429 Rate Limit
    # Second call: 429 Rate Limit
    # Third call: Success
    call_counts = 0

    def mock_call_model(*args, **kwargs):
        nonlocal call_counts
        call_counts += 1
        if call_counts <= 2:
            raise ProviderError("Rate limit reached", status_code=429, retry_after=0.01, is_retryable=True)
        # 3rd invocation: return valid repair block
        patch_text = (
            "<<<<<<< SEARCH\n"
            "def compute():\n"
            "    return 10\n"
            "=======\n"
            "def compute():\n"
            "    return 20\n"
            ">>>>>>> REPLACE"
        )
        return (patch_text, 120, 45, 0)

    controller._call_model = mock_call_model

    outcome = controller.run(
        target_file=src_file,
        test_command=f"pytest {test_file.name}",
        workspace_root=tmp_path,
        global_budget=global_budget,
        mock_test=True,
    )

    # 429s did NOT consume repair attempts! Bug resolved on attempt 1
    assert outcome.status == "SUCCESS"
    assert outcome.total_attempts == 1
    assert len(outcome.history) == 1
    assert call_counts == 3
    assert outcome.total_cost_usd > 0.0
    # Spend uncertain is False because only successful call was settled
    assert outcome.spend_uncertain is False


def test_rate_limit_backoff_ceiling_exceeded(tmp_path: Path):
    """When cumulative 429 backoff exceeds rate_limit_backoff_max_seconds, fail with PROVIDER_RATE_LIMITED."""
    src_file = tmp_path / "retry_mod.py"
    src_file.write_text("def x(): return 1\n", encoding="utf-8")
    test_file = tmp_path / "test_retry_mod.py"
    test_file.write_text("from retry_mod import x\ndef test_x(): assert x() == 2\n", encoding="utf-8")

    cfg = ArgusConfig(
        provider="custom",
        model="openai/gpt-oss-20b",
        budget_limit_usd=0.030,
        rate_limit_backoff_max_seconds=0.02,  # Very small backoff budget
    )
    controller = RecoveryController(config=cfg)

    def continuous_429(*args, **kwargs):
        raise ProviderError("Rate limit reached", status_code=429, retry_after=0.05, is_retryable=True)

    controller._call_model = continuous_429

    outcome = controller.run(
        target_file=src_file,
        test_command=f"pytest {test_file.name}",
        workspace_root=tmp_path,
        mock_test=True,
    )

    assert outcome.status == "PROVIDER_RATE_LIMITED"
    assert "Rate limit backoff ceiling" in outcome.message
    assert outcome.total_attempts == 0
    assert outcome.total_cost_usd == 0.0


# =============================================================================
# 6. Separate Timeout Policies
# =============================================================================

def test_separate_timeout_policies():
    """Verify distinct timeout limits for provider inference vs subprocess test execution."""
    cfg = ArgusConfig(
        provider="custom",
        provider_timeout_seconds=45.0,
        test_timeout_seconds=30,
    )
    controller = RecoveryController(config=cfg)

    assert controller.config.provider_timeout_seconds == 45.0
    assert controller.config.test_timeout_seconds == 30
    assert controller.test_runner.timeout_seconds == 30

    adapter = controller._get_adapter()
    assert adapter.config.timeout_seconds == 45.0
