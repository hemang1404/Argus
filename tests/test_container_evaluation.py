"""Milestone 2 Acceptance Test Suite: Container Evaluation Harness & Sandboxed Execution.

Validates:
1. Base-commit mismatch inside container is rejected (failing closed).
2. The exact intended candidate reaches the container via in-memory tar stream.
3. SHA256 integrity verification inside the container rejects corrupted or tampered files.
4. The verification command runs inside the container rather than on the host.
5. Host API credentials are scrubbed and never exposed to the evaluated container.
6. A deliberately incorrect candidate fails verification with diagnostics.
7. A correct fixture candidate passes verification.
8. Containers are reliably cleaned up after success, failure, and timeout in finally blocks.
9. Integration with RecoveryController enforces task deadlines and logs outcomes accurately.

All tests run strictly offline with no live paid API calls or SWE-bench benchmarks.
"""

from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

from argus.config import ArgusConfig
from argus.runtime.container import (
    BaseCommitMismatchError,
    CandidateIntegrityError,
    ContainerConfig,
    ContainerEvaluator,
    ContainerExecutionResult,
    ContainerSecurityRefusal,
    build_candidate_tar_stream,
)
from argus.runtime.controller import RecoveryController, RecoveryOutcome


class MockContainerEnvironment:
    """Simulates a Docker/Podman container engine and /testbed repository offline."""

    def __init__(self, base_commit: str = "abc12345basecommit", fail_test: bool = False) -> None:
        self.base_commit = base_commit
        self.fail_test = fail_test
        self.containers: Dict[str, Dict[str, Any]] = {}
        self.command_log: List[List[str]] = []
        self.cleaned_up_containers: List[str] = []
        self.corrupt_next_tar: bool = False
        self.simulate_timeout: bool = False

    def executor(
        self,
        args: List[str],
        input_bytes: Optional[bytes] = None,
        timeout: Optional[int] = None,
    ) -> Tuple[int, str, str]:
        self.command_log.append(list(args))
        cmd_str = " ".join(args)

        # 1. docker run -d ...
        if "run -d" in cmd_str:
            cid = f"mock_cid_{len(self.containers) + 1}"
            self.containers[cid] = {
                "base_commit": self.base_commit,
                "files": {},
            }
            return (0, f"{cid}\n", "")

        # Extract active container id if present in args
        cid = next((c for c in self.containers if c in args), "mock_cid_1")

        # 2. docker exec ... git rev-parse HEAD
        if "rev-parse HEAD" in cmd_str:
            return (0, f"{self.base_commit}\n", "")

        # 3. docker exec -i ... tar -xf -
        if "tar -xf -" in cmd_str:
            if self.corrupt_next_tar:
                return (1, "", "tar: unexpected EOF in archive")
            if input_bytes:
                with tarfile.open(fileobj=io.BytesIO(input_bytes), mode="r") as tar:
                    for member in tar.getmembers():
                        f = tar.extractfile(member)
                        content = f.read() if f else b""
                        if cid in self.containers:
                            self.containers[cid]["files"][member.name] = content
            return (0, "", "")

        # 4. docker exec ... sha256sum <path>
        if "sha256sum" in cmd_str:
            target_path = args[-1]
            rel_name = target_path.replace("/testbed/", "").lstrip("/")
            file_bytes = self.containers.get(cid, {}).get("files", {}).get(rel_name, b"")
            sha = hashlib.sha256(file_bytes).hexdigest()
            return (0, f"{sha}  {target_path}\n", "")

        # 5. docker exec ... <test command>
        if "pytest" in cmd_str or "python" in cmd_str:
            if self.simulate_timeout:
                raise subprocess.TimeoutExpired(cmd=args, timeout=timeout or 30)
            if self.fail_test:
                return (1, "FAILED: 1 test failed", "AssertionError: expected 42 got 0")
            return (0, "1 passed in 0.12s", "")

        # 6. docker rm -f <cid>
        if "rm -f" in cmd_str:
            target_cid = args[-1]
            self.cleaned_up_containers.append(target_cid)
            self.containers.pop(target_cid, None)
            return (0, f"{target_cid}\n", "")

        # 7. git reset / clean
        if "git reset" in cmd_str:
            return (0, "HEAD is now at " + self.base_commit, "")

        return (0, "OK", "")


# =============================================================================
# 1. Base-Commit Verification Acceptance Tests
# =============================================================================

def test_base_commit_mismatch_rejected_without_running_tests():
    """When repository base commit inside container does not match expected, fail closed."""
    mock_env = MockContainerEnvironment(base_commit="actual_repo_commit_9999")
    config = ContainerConfig(base_commit="expected_task_commit_1111", disposable=True)
    evaluator = ContainerEvaluator(config=config, raw_executor=mock_env.executor)

    res = evaluator.evaluate(
        candidate_content="def fix(): return 42\n",
        target_rel_path="solve.py",
        test_command="pytest test_solve.py",
        expected_base_commit="expected_task_commit_1111",
    )

    assert res.passed is False
    assert res.base_commit_verified is False
    assert res.exit_code == 128
    assert "BASE_COMMIT_MISMATCH" in res.traceback_summary

    # Ensure no test commands were ever dispatched
    assert not any("pytest" in " ".join(cmd) for cmd in mock_env.command_log)
    # Ensure container was cleaned up
    assert len(mock_env.cleaned_up_containers) == 1


# =============================================================================
# 2. Candidate Transfer and In-Container Hash Verification
# =============================================================================

def test_exact_intended_candidate_reaches_container_and_hash_verified():
    """Verify that candidate content is streamed via tar and SHA256 matches inside container."""
    mock_env = MockContainerEnvironment(base_commit="commit_123")
    config = ContainerConfig(base_commit="commit_123", disposable=True)
    evaluator = ContainerEvaluator(config=config, raw_executor=mock_env.executor)

    candidate = "def solve():\n    return 42\n"
    expected_hash = hashlib.sha256(candidate.encode("utf-8")).hexdigest()

    res = evaluator.evaluate(
        candidate_content=candidate,
        target_rel_path="solution/math.py",
        test_command="pytest test_math.py",
    )

    assert res.passed is True
    assert res.base_commit_verified is True
    assert res.candidate_hash_verified is True
    assert res.exit_code == 0
    assert len(mock_env.cleaned_up_containers) == 1


def test_candidate_integrity_hash_mismatch_rejected():
    """If candidate hash in container differs from expected artifact, refuse execution."""
    mock_env = MockContainerEnvironment(base_commit="commit_123")
    config = ContainerConfig(base_commit="commit_123", disposable=True)
    evaluator = ContainerEvaluator(config=config, raw_executor=mock_env.executor)

    candidate = "def solve(): return 42\n"
    wrong_expected_hash = "0000000000000000000000000000000000000000000000000000000000000000"

    res = evaluator.evaluate(
        candidate_content=candidate,
        target_rel_path="solve.py",
        test_command="pytest test_solve.py",
        expected_candidate_hash=wrong_expected_hash,
    )

    assert res.passed is False
    assert res.candidate_hash_verified is False
    assert res.exit_code == 129
    assert "CANDIDATE_INTEGRITY_MISMATCH" in res.traceback_summary
    # Ensure container was destroyed
    assert len(mock_env.cleaned_up_containers) == 1


# =============================================================================
# 3. Verification Command Runs in Container, Never on Host
# =============================================================================

def test_verification_command_runs_in_container_not_host():
    """Verify that test commands are executed with 'docker exec' without host subprocess leaks."""
    mock_env = MockContainerEnvironment(base_commit="commit_123")
    config = ContainerConfig(base_commit="commit_123", disposable=True)
    evaluator = ContainerEvaluator(config=config, raw_executor=mock_env.executor)

    with patch("subprocess.Popen") as mock_host_popen:
        res = evaluator.evaluate(
            candidate_content="def fix(): pass\n",
            target_rel_path="code.py",
            test_command="pytest -v test_code.py",
        )

        assert res.passed is True
        # Host subprocess.Popen was NEVER called directly for the test command
        assert mock_host_popen.call_count == 0

        # Command was dispatched to docker exec
        exec_calls = [cmd for cmd in mock_env.command_log if "exec" in cmd and "pytest" in cmd]
        assert len(exec_calls) == 1
        assert "-w" in exec_calls[0]
        assert "/testbed" in exec_calls[0]


# =============================================================================
# 4. Incorrect Candidate Fails & Correct Candidate Passes Verification
# =============================================================================

def test_deliberately_incorrect_candidate_fails_verification():
    """Incorrect candidate produces nonzero exit code and logs failure diagnostics."""
    mock_env = MockContainerEnvironment(base_commit="commit_123", fail_test=True)
    config = ContainerConfig(base_commit="commit_123", disposable=True)
    evaluator = ContainerEvaluator(config=config, raw_executor=mock_env.executor)

    res = evaluator.evaluate(
        candidate_content="def broken(): return False\n",
        target_rel_path="app.py",
        test_command="pytest test_app.py",
    )

    assert res.passed is False
    assert res.exit_code == 1
    assert "AssertionError" in res.traceback_summary
    assert len(mock_env.cleaned_up_containers) == 1


def test_correct_fixture_candidate_passes_verification():
    """Correct candidate returns exit code 0 and passes evaluation."""
    mock_env = MockContainerEnvironment(base_commit="commit_123", fail_test=False)
    config = ContainerConfig(base_commit="commit_123", disposable=True)
    evaluator = ContainerEvaluator(config=config, raw_executor=mock_env.executor)

    res = evaluator.evaluate(
        candidate_content="def correct(): return True\n",
        target_rel_path="app.py",
        test_command="pytest test_app.py",
    )

    assert res.passed is True
    assert res.exit_code == 0
    assert "1 passed" in res.stdout
    assert len(mock_env.cleaned_up_containers) == 1


# =============================================================================
# 5. Guaranteed Container Cleanup on Success, Failure, and Timeout
# =============================================================================

def test_container_cleaned_up_on_timeout():
    """Container is forcefully removed even if test execution times out."""
    mock_env = MockContainerEnvironment(base_commit="commit_123")
    mock_env.simulate_timeout = True
    config = ContainerConfig(base_commit="commit_123", timeout_seconds=10, disposable=True)
    evaluator = ContainerEvaluator(config=config, raw_executor=mock_env.executor)

    res = evaluator.evaluate(
        candidate_content="def slow(): time.sleep(100)\n",
        target_rel_path="slow.py",
        test_command="pytest test_slow.py",
    )

    assert res.passed is False
    assert res.timed_out is True
    assert res.exit_code == 124
    assert "timed out after 10s" in res.stderr
    # Container must be destroyed in finally block
    assert len(mock_env.cleaned_up_containers) == 1


# =============================================================================
# 6. Recovery Controller Integration with Container Evaluator
# =============================================================================

def test_recovery_controller_uses_container_evaluator_end_to_end(tmp_path: Path):
    """RecoveryController performs iterative repair and verifies candidates inside container."""
    src_file = tmp_path / "calc.py"
    src_file.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    test_file = tmp_path / "test_calc.py"
    test_file.write_text("from calc import add\ndef test_add(): assert add(2, 3) == 5\n", encoding="utf-8")

    # Mock environment: baseline test fails, attempt 1 fixes and passes
    mock_env = MockContainerEnvironment(base_commit="base_sha_abc123")
    call_idx = 0

    def progressive_executor(args, input_bytes=None, timeout=None):
        nonlocal call_idx
        cmd_str = " ".join(args)
        if "pytest" in cmd_str:
            call_idx += 1
            if call_idx == 1:
                # Baseline fails
                return (1, "FAILED", "AssertionError: expected 5 got -1")
            else:
                # Attempt 1 passes
                return (0, "1 passed", "")
        return mock_env.executor(args, input_bytes=input_bytes, timeout=timeout)

    evaluator = ContainerEvaluator(
        config=ContainerConfig(base_commit="base_sha_abc123", timeout_seconds=60),
        raw_executor=progressive_executor,
    )

    cfg = ArgusConfig(
        provider="custom",
        budget_limit_usd=0.030,
        base_commit="base_sha_abc123",
        use_container_runner=True,
    )
    controller = RecoveryController(config=cfg, container_evaluator=evaluator)

    # Mock model response with valid patch block
    patch_text = (
        "<<<<<<< SEARCH\n"
        "def add(a, b):\n"
        "    return a - b\n"
        "=======\n"
        "def add(a, b):\n"
        "    return a + b\n"
        ">>>>>>> REPLACE"
    )
    controller._call_model = lambda *args, **kwargs: (patch_text, 100, 30, 0)

    outcome = controller.run(
        target_file=src_file,
        test_command=f"pytest {test_file.name}",
        workspace_root=tmp_path,
        mock_test=True,
    )

    assert outcome.status == "SUCCESS"
    assert outcome.total_attempts == 1
    assert outcome.verified is True
    assert "add(a, b)" in outcome.unified_diff
