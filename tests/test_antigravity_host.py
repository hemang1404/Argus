"""Automated tests for Antigravity host-native adaptive routing and recovery."""

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from argus.config import ArgusConfig
from argus.providers.adapter import CompletionResponse, ConnectionTestResult, OpenAICompatibleAdapter, ProviderError
from argus.providers.antigravity import AntigravityHostAdapter, find_antigravity_cli
from argus.providers.models import ProviderConfig
from argus.routing.difficulty import DifficultyClassifier, DifficultyDecision
from argus.runtime.candidate import AuthoritativePatchApplier, CandidateArtifactStore
from argus.runtime.controller import RecoveryController, RecoveryOutcome


# =============================================================================
# 1. Difficulty Routing Tests (Low, Medium, High)
# =============================================================================

def test_difficulty_routing_low():
    """Verify simple questions, typo fixes, and docstrings route to low effort."""
    classifier = DifficultyClassifier()

    d1 = classifier.classify("What is the meaning of this configuration option?")
    assert d1.effort == "low"
    assert d1.confidence >= 0.85
    assert any("inquiry" in r.lower() for r in d1.reasons)

    d2 = classifier.classify("Fix typo in the variable name", code="x = 1\ny = 2\n")
    assert d2.effort == "low"

    d3 = classifier.classify("Add docstring to calculate_total function")
    assert d3.effort == "low"


def test_difficulty_routing_medium():
    """Verify standard single-file bug fixes and ordinary features route to medium effort."""
    classifier = DifficultyClassifier()

    # Bug fix with standard AssertionError and 1-2 traceback frames
    error_tb = (
        'Traceback (most recent call last):\n'
        '  File "test_calc.py", line 12, in test_add\n'
        '    assert add(2, 2) == 5\n'
        'AssertionError: assert 4 == 5\n'
    )
    d1 = classifier.classify(
        prompt="Fix failing unit test in calculate_sum",
        code="def add(a, b):\n    return a + b + 1\n",
        error_feedback=error_tb,
    )
    assert d1.effort == "medium"
    assert d1.confidence >= 0.75

    # Standard feature implementation without errors
    d2 = classifier.classify(
        prompt="Implement helper function to parse user email",
        code="def parse_email(val):\n    pass\n",
    )
    assert d2.effort == "medium"


def test_difficulty_routing_high():
    """Verify multi-file scope, deep tracebacks, severe errors, and architecture route to high effort."""
    classifier = DifficultyClassifier()

    # 1. Deep traceback (>= 4 frames)
    deep_tb = (
        'Traceback (most recent call last):\n'
        '  File "server.py", line 40, in handle\n'
        '  File "pipeline.py", line 85, in process\n'
        '  File "encoder.py", line 112, in encode\n'
        '  File "core.py", line 19, in transform\n'
        'ValueError: corrupted buffer state\n'
    )
    d1 = classifier.classify(prompt="Fix server pipeline failure", error_feedback=deep_tb)
    assert d1.effort == "high"
    assert any("deep traceback" in r.lower() for r in d1.reasons)

    # 2. Severe exception type (RecursionError)
    recursion_tb = (
        'Traceback (most recent call last):\n'
        '  File "tree.py", line 10, in traverse\n'
        'RecursionError: maximum recursion depth exceeded in comparison\n'
    )
    d2 = classifier.classify(prompt="Fix tree traversal bug", error_feedback=recursion_tb)
    assert d2.effort == "high"
    assert any("severe exception" in r.lower() for r in d2.reasons)

    # 3. Multi-file scope keywords and multiple files
    d3 = classifier.classify(
        prompt="Refactor authentication across multiple files in the repository",
        files=["auth/login.py", "auth/token.py", "api/routes.py"],
    )
    assert d3.effort == "high"
    assert any("multi-file" in r.lower() for r in d3.reasons)

    # 4. Architectural concurrency / redesign
    d4 = classifier.classify(
        prompt="Redesign the connection manager to handle asyncio concurrency and deadlocks",
        code="class Manager:\n" + ("    pass\n" * 60),
    )
    assert d4.effort == "high"


def test_difficulty_routing_explicit_override():
    """Verify explicit user override takes precedence over heuristic scoring."""
    classifier = DifficultyClassifier()

    # Even on a trivial prompt, explicit 'high' forces high effort
    d_high = classifier.classify("What is this?", explicit_override="high")
    assert d_high.effort == "high"
    assert d_high.confidence == 1.0
    assert "override" in d_high.reasons[0].lower()

    # Even on a deep recursion failure, explicit 'low' forces low effort
    d_low = classifier.classify(
        prompt="Refactor multi-file architecture",
        error_feedback="RecursionError: maximum recursion depth exceeded",
        explicit_override="low",
    )
    assert d_low.effort == "low"
    assert d_low.confidence == 1.0


def test_difficulty_routing_disabled():
    """Verify disabling adaptive routing returns conservative medium fallback."""
    classifier = DifficultyClassifier()

    d1 = classifier.classify("What is this?", disable_adaptive=True)
    assert d1.effort == "medium"
    assert d1.confidence == 1.0
    assert "disabled" in d1.reasons[0].lower()

    with patch.dict(os.environ, {"ARGUS_DISABLE_ADAPTIVE_ROUTING": "1"}):
        d2 = classifier.classify("What is this?")
        assert d2.effort == "medium"
        assert d2.confidence == 1.0


# =============================================================================
# 2. Host CLI Detection, Missing, and Authentication Failure Tests
# =============================================================================

def test_antigravity_cli_missing_raises_clear_error(monkeypatch):
    """Verify missing CLI produces actionable, descriptive ProviderError."""
    monkeypatch.setenv("ARGUS_AGY_BIN", "/non/existent/path/to/agy")
    monkeypatch.setattr(shutil, "which", lambda cmd: None)

    adapter = AntigravityHostAdapter(cli_path="/non/existent/path/to/agy")
    with pytest.raises(ProviderError) as exc_info:
        adapter.chat_completion(messages=[{"role": "user", "content": "hello"}])

    assert exc_info.value.status_code == 404
    assert "not found" in str(exc_info.value).lower()
    assert "argus_agy_bin" in str(exc_info.value).lower()

    # Verify test_connection also returns clean ConnectionTestResult
    res = adapter.test_connection()
    assert not res.success
    assert res.status_code == 404
    assert "not found" in res.message.lower()


def test_antigravity_cli_unauthenticated(tmp_path):
    """Verify unauthenticated host CLI reports 401 and guides user to authenticate."""
    if os.name == "nt":
        fake_cli = tmp_path / "fake_agy.bat"
        fake_cli.write_text("@echo off\necho Error: unauthenticated. Login required to access Antigravity.\nexit /b 1\n")
    else:
        fake_cli = tmp_path / "fake_agy.sh"
        fake_cli.write_text("#!/bin/sh\necho 'Error: unauthenticated. Login required to access Antigravity.'\nexit 1\n")
        fake_cli.chmod(0o755)

    adapter = AntigravityHostAdapter(cli_path=fake_cli)
    conn_res = adapter.test_connection()
    assert not conn_res.success
    assert conn_res.status_code == 401
    assert "unauthenticated" in conn_res.message.lower()

    with pytest.raises(ProviderError) as exc_info:
        adapter.chat_completion(messages=[{"role": "user", "content": "test"}])
    assert exc_info.value.status_code == 401
    assert "authentication error" in str(exc_info.value).lower()


# =============================================================================
# 3. Invocation Argument Safety, Cancellation, Timeout, and Fallback
# =============================================================================

def test_invocation_argument_safety_and_no_shell(tmp_path):
    """Verify subprocess is invoked as an argument list with shell=False and effort flag."""
    fake_cli = tmp_path / "fake_agy.bat"
    fake_cli.write_text("@echo off\necho Success response\n")

    adapter = AntigravityHostAdapter(cli_path=fake_cli)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Valid repair response",
            stderr="",
        )

        resp = adapter.chat_completion(
            messages=[{"role": "user", "content": "Fix bug in calc.py"}],
            model="claude-sonnet-4-6",
            effort="high",
        )

        assert mock_run.called
        call_args, call_kwargs = mock_run.call_args

        # CRITICAL SAFETY REQUIREMENT: shell must be False
        assert call_kwargs.get("shell") is False
        # Arguments must be a list
        args_passed = call_args[0]
        assert isinstance(args_passed, list)
        assert str(fake_cli) in args_passed[0]
        assert "--effort" in args_passed
        assert "high" in args_passed
        assert "-p" in args_passed
        assert resp.content == "Valid repair response"


def test_invocation_timeout_handling(tmp_path):
    """Verify timeout produces ProviderError with status 504 and sanitized message."""
    fake_cli = tmp_path / "fake_agy.bat"
    fake_cli.write_text("@echo off\n")

    adapter = AntigravityHostAdapter(cli_path=fake_cli, timeout_seconds=2.0)

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["agy"], timeout=2.0)):
        with pytest.raises(ProviderError) as exc_info:
            adapter.chat_completion(messages=[{"role": "user", "content": "Do work"}])
        assert exc_info.value.status_code == 504
        assert "timed out after 2.0s" in str(exc_info.value)


def test_antigravity_cli_non_zero_exit_returns_failure(tmp_path):
    """Verify test_connection returns success=False and 502 if CLI exits with non-zero code."""
    fake_cli = tmp_path / "fake_agy.bat"
    fake_cli.write_text("@echo off\n")

    adapter = AntigravityHostAdapter(cli_path=fake_cli)

    fail_proc = subprocess.CompletedProcess(
        args=["agy", "models"],
        returncode=1,
        stdout="",
        stderr="Fatal error: could not locate backend service",
    )
    with patch("subprocess.run", return_value=fail_proc):
        conn = adapter.test_connection()
        assert conn.success is False
        assert conn.status_code == 502
        assert "probe failed" in conn.message



def test_effort_fallback_when_unsupported_by_cli(tmp_path):
    """Verify graceful fallback when CLI reports unknown flag for --effort."""
    fake_cli = tmp_path / "fake_agy.bat"
    fake_cli.write_text("@echo off\n")

    adapter = AntigravityHostAdapter(cli_path=fake_cli)

    # First call fails with "unknown flag: --effort"
    fail_proc = subprocess.CompletedProcess(
        args=["agy", "--effort", "high", "-p", "fix"],
        returncode=1,
        stdout="",
        stderr="error: unknown flag: --effort",
    )
    # Second call succeeds without --effort
    success_proc = subprocess.CompletedProcess(
        args=["agy", "-p", "fix"],
        returncode=0,
        stdout="Fallback successful response",
        stderr="",
    )

    with patch("subprocess.run", side_effect=[fail_proc, success_proc]) as mock_run:
        resp = adapter.chat_completion(
            messages=[{"role": "user", "content": "fix bug"}],
            model="claude-sonnet-4-6",
            effort="high",
        )
        assert resp.content == "Fallback successful response"
        assert mock_run.call_count == 2
        # Verify second call omitted --effort
        second_args = mock_run.call_args_list[1][0][0]
        assert "--effort" not in second_args


def test_response_normalization(tmp_path):
    """Verify response is normalized to standard CompletionResponse with token estimation."""
    fake_cli = tmp_path / "fake_agy.bat"
    fake_cli.write_text("@echo off\n")

    adapter = AntigravityHostAdapter(cli_path=fake_cli)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE",
            stderr="",
        )

        resp = adapter.chat_completion(
            messages=[{"role": "user", "content": "Please change x = 1 to x = 2"}],
            effort="low",
        )

        assert isinstance(resp, CompletionResponse)
        assert "gemini-3.8-flash" in resp.model
        assert resp.prompt_tokens > 0
        assert resp.completion_tokens > 0
        assert resp.duration_ms >= 0.0
        assert "SEARCH" in resp.content


# =============================================================================
# 4. Bounded Recovery with Workspace Isolation and Human Approval Gate
# =============================================================================

def test_antigravity_recovery_in_isolated_workspace(tmp_path):
    """Verify failing tests trigger bounded recovery using Antigravity host adapter.
    
    Validates:
    - Task difficulty classification.
    - Isolated workspace creation.
    - Patch validation and AST verification.
    - Candidate creation without modifying original source file.
    - Candidate approval gating.
    """
    repo = tmp_path / "toy_repo"
    repo.mkdir()
    target_file = repo / "calculator.py"
    target_file.write_text("def multiply(a, b):\n    return a + b  # Bug: should be a * b\n", encoding="utf-8")

    test_file = repo / "test_calculator.py"
    test_file.write_text(
        "from calculator import multiply\n"
        "def test_multiply():\n"
        "    assert multiply(3, 4) == 12\n",
        encoding="utf-8",
    )

    # Create mock Antigravity host adapter returning the correct search/replace block
    mock_patch = (
        "<<<<<<< SEARCH\n"
        "def multiply(a, b):\n"
        "    return a + b  # Bug: should be a * b\n"
        "=======\n"
        "def multiply(a, b):\n"
        "    return a * b\n"
        ">>>>>>> REPLACE"
    )
    fake_adapter = MagicMock()
    fake_adapter.chat_completion.return_value = CompletionResponse(
        content=mock_patch,
        prompt_tokens=40,
        completion_tokens=20,
        duration_ms=150.0,
        model="gemini-2.5-pro",
    )

    cfg = ArgusConfig(
        provider="antigravity",
        model="gemini-2.5-pro",
        max_attempts=2,
    )
    controller = RecoveryController(config=cfg, adapter=fake_adapter)

    outcome = controller.run(
        target_file=target_file,
        test_command=f"pytest {test_file.name}",
        test_file=test_file,
        workspace_root=repo,
    )

    # 1. Verification of outcome
    assert outcome.status == "SUCCESS"
    assert outcome.verified is True
    assert outcome.total_attempts == 1
    assert outcome.effort_used in ("low", "medium", "high")
    assert bool(outcome.unified_diff)

    # 2. Workspace isolation check: original source file must remain UNTOUCHED!
    original_content = target_file.read_text(encoding="utf-8")
    assert "return a + b" in original_content, "Original file was modified before approval!"

    # 3. Candidate approval enforcement: unapproved candidate must be rejected
    run_id = outcome.run_id
    unapproved_apply = AuthoritativePatchApplier.apply(
        run_id=run_id,
        workspace_root=repo,
        require_approval=True,
    )
    assert unapproved_apply.success is False
    assert unapproved_apply.error_code == "CANDIDATE_NOT_APPROVED"

    # 4. Explicit human approval granting
    approval_res = CandidateArtifactStore.set_approval(run_id=run_id, approved=True, workspace_root=repo)
    assert approval_res.approved is True

    # 5. Authoritative application of approved candidate
    approved_apply = AuthoritativePatchApplier.apply(
        run_id=run_id,
        workspace_root=repo,
        require_approval=True,
    )
    assert approved_apply.success is True

    # 6. Verify original file now has the passing fix
    modified_content = target_file.read_text(encoding="utf-8")
    assert "return a * b" in modified_content


def test_clean_baseline_reset_between_failed_attempts(tmp_path):
    """Verify workspace is reset to clean baseline between failed patch attempts."""
    repo = tmp_path / "toy_repo"
    repo.mkdir()
    target_file = repo / "math_ops.py"
    target_file.write_text("def divide(a, b):\n    return 0\n", encoding="utf-8")

    test_file = repo / "test_math_ops.py"
    test_file.write_text(
        "from math_ops import divide\n"
        "def test_divide():\n"
        "    assert divide(10, 2) == 5\n",
        encoding="utf-8",
    )

    # Attempt 1 returns broken syntax patch; Attempt 2 returns passing patch
    bad_patch = (
        "<<<<<<< SEARCH\n"
        "    return 0\n"
        "=======\n"
        "    return 100\n"  # Still fails test
        ">>>>>>> REPLACE"
    )
    good_patch = (
        "<<<<<<< SEARCH\n"
        "    return 0\n"
        "=======\n"
        "    return a // b\n"
        ">>>>>>> REPLACE"
    )

    fake_adapter = MagicMock()
    fake_adapter.chat_completion.side_effect = [
        CompletionResponse(content=bad_patch, prompt_tokens=30, completion_tokens=15, duration_ms=100.0, model="gemini-2.5-pro"),
        CompletionResponse(content=good_patch, prompt_tokens=35, completion_tokens=15, duration_ms=100.0, model="gemini-2.5-pro"),
    ]

    cfg = ArgusConfig(provider="antigravity", model="gemini-2.5-pro", max_attempts=3)
    controller = RecoveryController(config=cfg, adapter=fake_adapter)

    outcome = controller.run(
        target_file=target_file,
        test_command=f"pytest {test_file.name}",
        test_file=test_file,
        workspace_root=repo,
    )

    assert outcome.status == "SUCCESS"
    assert outcome.total_attempts == 2
    assert len(outcome.history) == 2
    assert outcome.history[0].test_passed is False
    assert outcome.history[1].test_passed is True


# =============================================================================
# 5. Direct-API Provider Non-Regression Check
# =============================================================================

def test_direct_api_provider_remains_functional():
    """Verify OpenAICompatibleAdapter and direct API providers remain functional without regression."""
    p_cfg = ProviderConfig(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        api_key="sk_mock_test_key_for_offline_validation",
    )
    adapter = OpenAICompatibleAdapter(config=p_cfg)
    assert adapter.config.name == "deepseek"
    assert adapter.config.masked_key == "sk_m...tion"

    cfg = ArgusConfig(provider="deepseek", provider_config=p_cfg)
    controller = RecoveryController(config=cfg)
    # Controller lazily initializes OpenAICompatibleAdapter for deepseek
    active_adapter = controller._get_adapter()
    assert isinstance(active_adapter, OpenAICompatibleAdapter)
    assert active_adapter.config.name == "deepseek"


# =============================================================================
# 6. Tool-Permission Boundary & Real-Time Incremental Streaming Tests
# =============================================================================

def test_tool_permission_boundary_and_worktree_isolation(tmp_path):
    """Verify that AntigravityHostAdapter strictly injects safety flags and isolates cwd."""
    fake_cli = tmp_path / "fake_agy.bat"
    fake_cli.write_text("@echo off\necho {}\n")

    worktree_dir = tmp_path / "isolated_worktree"
    worktree_dir.mkdir()

    adapter = AntigravityHostAdapter(cli_path=fake_cli)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"status": "SUCCESS", "response": "ok", "usage": {"input_tokens": 10, "output_tokens": 5}}',
            stderr="",
        )

        resp = adapter.chat_completion(
            messages=[{"role": "user", "content": "Hello"}],
            cwd=worktree_dir,
        )

        assert mock_run.called
        call_args, call_kwargs = mock_run.call_args

        args_passed = call_args[0]
        # Verified safety flags:
        assert "--sandbox" in args_passed
        assert "--disable-slash-commands" in args_passed
        assert "--dangerously-skip-permissions" not in args_passed

        # Verified working directory isolation:
        assert call_kwargs.get("cwd") == str(worktree_dir.resolve())
        assert resp.content == "ok"


def test_stream_chat_completion_real_time_incremental_deltas(tmp_path):
    """Verify that stream_chat_completion extracts incremental text deltas from stream-json events."""
    fake_cli = tmp_path / "fake_agy.bat"
    fake_cli.write_text("@echo off\n")

    adapter = AntigravityHostAdapter(cli_path=fake_cli)

    stream_json_lines = (
        '{"event":"init","init":{"model":"gemini-3.8-flash-low"}}\n'
        '{"event":"step_update","step_update":{"step_index":0,"state":"DONE","step_type":"user_input"}}\n'
        '{"event":"step_update","step_update":{"step_index":1,"state":"ACTIVE","step_type":"agent_response","text_delta":"delta_one "}}\n'
        '{"event":"step_update","step_update":{"step_index":1,"state":"ACTIVE","step_type":"agent_response","text_delta":"delta_two "}}\n'
        '{"event":"step_update","step_update":{"step_index":1,"state":"DONE","step_type":"agent_response","text_delta":"delta_three"}}\n'
        '{"event":"result","result":{"status":"SUCCESS","response":"delta_one delta_two delta_three"}}\n'
    )

    class MockProc:
        def __init__(self):
            import io
            self.stdout = io.StringIO(stream_json_lines)
            self.stderr = io.StringIO("")
            self.returncode = 0
        def wait(self, timeout=None):
            return 0
        def kill(self):
            pass

    with patch("subprocess.Popen", return_value=MockProc()) as mock_popen:
        deltas = list(adapter.stream_chat_completion(
            messages=[{"role": "user", "content": "Count"}],
            cwd=tmp_path,
        ))

        assert mock_popen.called
        popen_args, popen_kwargs = mock_popen.call_args
        args_passed = popen_args[0]

        assert "--output-format" in args_passed
        assert "stream-json" in args_passed
        assert "--sandbox" in args_passed
        assert "--disable-slash-commands" in args_passed
        assert popen_kwargs.get("cwd") == str(tmp_path.resolve())

        # Verify true incremental deltas
        assert deltas == ["delta_one ", "delta_two ", "delta_three"]
        assert "".join(deltas) == "delta_one delta_two delta_three"
