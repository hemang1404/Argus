"""Unit tests for Argus Auto-Localization and Multi-Tier Escalation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from argus.config import ArgusConfig
from argus.providers.adapter import CompletionResponse, ProviderAdapter, ConnectionTestResult
from argus.runtime.controller import RecoveryController, AttemptRecord


class MockAdapterWithEscalationTracking(ProviderAdapter):
    """Mock adapter that records which model and effort was used on each call."""

    def __init__(self, responses: list[str]):
        self.responses = responses
        self.call_history: list[dict] = []

    def chat_completion(
        self,
        messages: list[dict],
        temperature: float = None,
        max_tokens: int = None,
        effort: str = None,
        model: str = None,
        **kwargs,
    ) -> CompletionResponse:
        call_idx = len(self.call_history)
        self.call_history.append({
            "model": model,
            "effort": effort,
            "messages": messages,
        })
        content = self.responses[call_idx] if call_idx < len(self.responses) else "No patch."
        return CompletionResponse(
            content=content,
            prompt_tokens=100,
            completion_tokens=50,
            duration_ms=45.0,
            model=model or "mock-model",
        )

    def test_connection(self) -> ConnectionTestResult:
        return ConnectionTestResult(
            success=True,
            status_code=200,
            latency_ms=10.0,
            model="mock",
            message="OK",
        )


@pytest.fixture
def test_repo_structure(tmp_path: Path):
    """Build a mini repo with source file and test file."""
    src_dir = tmp_path / "mypkg"
    src_dir.mkdir(parents=True)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True)

    math_file = src_dir / "calculator.py"
    math_file.write_text(
        "def compute_total(a, b):\n"
        "    # Bug: adds instead of multiplies\n"
        "    return a + b\n",
        encoding="utf-8",
    )

    test_file = tests_dir / "test_calculator.py"
    test_file.write_text(
        "from mypkg.calculator import compute_total\n"
        "def test_multiply():\n"
        "    assert compute_total(3, 4) == 12\n",
        encoding="utf-8",
    )

    return tmp_path, math_file, test_file


def test_auto_localize_target_file_from_traceback(test_repo_structure):
    """Verify that auto_localize_target_file extracts the correct source file from pytest traceback."""
    repo_dir, math_file, test_file = test_repo_structure
    controller = RecoveryController(config=ArgusConfig(provider="antigravity"))

    traceback_sample = (
        '=================================== FAILURES ===================================\n'
        '________________________________ test_multiply _________________________________\n'
        f'  File "{test_file}", line 3, in test_multiply\n'
        '    assert compute_total(3, 4) == 12\n'
        f'  File "{math_file}", line 3, in compute_total\n'
        '    return a + b\n'
        'AssertionError: assert 7 == 12\n'
    )

    localized = controller.auto_localize_target_file(
        repo_dir=repo_dir,
        failure_output=traceback_sample,
        test_command="pytest tests/test_calculator.py",
    )

    assert localized is not None
    assert localized.resolve() == math_file.resolve()


def test_auto_localize_target_file_from_symbols_when_no_source_in_traceback(test_repo_structure):
    """Verify that when the traceback only shows the test file, AST symbol table finds the definition."""
    repo_dir, math_file, test_file = test_repo_structure
    controller = RecoveryController(config=ArgusConfig(provider="antigravity"))

    # Traceback only mentions test_calculator.py
    traceback_sample = (
        f'  File "{test_file}", line 3, in test_multiply\n'
        '    assert compute_total(3, 4) == 12\n'
        'AssertionError: assert 7 == 12\n'
    )

    localized = controller.auto_localize_target_file(
        repo_dir=repo_dir,
        failure_output=traceback_sample,
        test_command="pytest tests/test_calculator.py",
    )

    assert localized is not None
    assert localized.resolve() == math_file.resolve()


def test_escalation_ladder_tiers_across_attempts(test_repo_structure):
    """Verify that RecoveryController modulates (model, effort, context) across turns 1, 2, and 3."""
    repo_dir, math_file, test_file = test_repo_structure

    # 3 attempts of responses:
    # Attempt 1: Syntax error / bad patch -> Fails
    # Attempt 2: Incorrect logic -> Fails
    # Attempt 3: Verified working fix -> Passes
    working_patch = (
        f"### File: mypkg/calculator.py\n"
        f"<<<<<<< SEARCH\n"
        f"    return a + b\n"
        f"=======\n"
        f"    return a * b\n"
        f">>>>>>> REPLACE"
    )
    responses = [
        "Synthesizing bad syntax <<<<<<< SEARCH",
        "def compute_total(a, b):\n    return a - b",
        working_patch,
    ]

    mock_adapter = MockAdapterWithEscalationTracking(responses)

    config = ArgusConfig(
        provider="antigravity",
        small_model="gemini-3.8-flash-low",
        large_model="gemini-3.8-flash-high",
        escalation_enabled=True,
        max_attempts=3,
        budget_limit_usd=1.0,
    )
    controller = RecoveryController(config=config, adapter=mock_adapter)

    outcome = controller.run(
        target_file=math_file,
        test_command="pytest tests/test_calculator.py",
        workspace_root=repo_dir,
    )

    assert outcome.status == "SUCCESS"
    assert outcome.verified is True
    assert outcome.total_attempts == 3

    # Check model and effort for each attempt in mock adapter history
    history = mock_adapter.call_history
    assert len(history) == 3

    # Attempt 1: small model, low/medium effort
    assert history[0]["model"] == "gemini-3.8-flash-low"

    # Attempt 2: small model, high effort (deep reasoning)
    assert history[1]["model"] == "gemini-3.8-flash-low"
    assert history[1]["effort"] == "high"

    # Attempt 3: escalated to large model (frontier tier)
    assert history[2]["model"] == "gemini-3.8-flash-high"
    assert history[2]["effort"] == "high"


def test_argus_recover_mcp_without_target_file(test_repo_structure):
    """Verify that argus_recover MCP tool succeeds when target_file is omitted."""
    from argus.mcp.server import create_mcp_server
    repo_dir, math_file, test_file = test_repo_structure

    server = create_mcp_server()

    working_patch = (
        f"<<<<<<< SEARCH\n"
        f"    return a + b\n"
        f"=======\n"
        f"    return a * b\n"
        f">>>>>>> REPLACE"
    )

    mock_adapter = MockAdapterWithEscalationTracking([working_patch])

    with patch("argus.mcp.server.ProviderStorage") as mock_storage_cls:
        mock_storage = MagicMock()
        mock_storage.get_active_provider.return_value = MagicMock(
            name="ollama",
            model="qwen2.5-coder:7b",
            base_url="http://127.0.0.1:11434/v1",
            api_key="",
            masked_key="(none)",
        )
        mock_storage_cls.return_value = mock_storage

        with patch.object(RecoveryController, "_get_adapter", return_value=mock_adapter):
            # Call argus_recover tool function directly from server
            recover_tool = server._tool_manager._tools["argus_recover"].fn

            raw_res = recover_tool(
                test_command="pytest tests/test_calculator.py",
                target_file=None,  # OMITTED!
                workspace=str(repo_dir),
            )

            res = json.loads(raw_res)
            assert res["status"] == "SUCCESS"
            assert res["verified"] is True
            assert "calculator.py" in res["target_file"]
