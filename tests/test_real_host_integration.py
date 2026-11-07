"""Real-host integration test for Antigravity host-native adaptive routing and recovery.

Labels: real-host integration test.
Exercises end-to-end task execution and test repair against the installed Antigravity CLI.
"""

import os
from pathlib import Path

import pytest

from argus.config import ArgusConfig
from argus.providers.antigravity import AntigravityHostAdapter, find_antigravity_cli
from argus.providers.models import ProviderConfig
from argus.runtime.candidate import AuthoritativePatchApplier, CandidateArtifactStore
from argus.runtime.controller import RecoveryController


@pytest.mark.real_host
def test_real_host_cli_task_execution():
    """Verify that Argus selects effort and executes via real Antigravity host CLI."""
    cli_path = find_antigravity_cli()
    if cli_path is None:
        pytest.skip("Antigravity host CLI not found on machine.")

    adapter = AntigravityHostAdapter(cli_path=cli_path)
    conn_result = adapter.test_connection()
    if not conn_result.success and conn_result.status_code == 401:
        pytest.skip("Antigravity host is unauthenticated.")

    assert conn_result.success is True, f"Host ping failed: {conn_result.message}"

    # Execute a task requesting low effort
    try:
        resp = adapter.chat_completion(
            messages=[{"role": "user", "content": "What is binary search?"}],
            effort="low",
        )
    except Exception as ex:
        if "project_id is required" in str(ex):
            pytest.skip("Antigravity host CLI requires an active IDE project session (project_id); headless CLI inference is unverified in this environment.")
        raise
    assert resp.content
    assert resp.model
    assert resp.raw_response.get("effort") == "low"
    assert resp.raw_response.get("exit_code") == 0


@pytest.mark.real_host
def test_real_host_deliberately_failing_toy_repo_recovery(tmp_path):
    """Real-host integration test:

    1. Sets up a deliberately failing toy repository with a bug.
    2. Argus evaluates task difficulty and routes inference with selected effort to Antigravity CLI.
    3. Triggers isolated bounded recovery.
    4. Verified patch is synthesized, passes in isolation, and candidate artifact is saved.
    5. Original file remains untouched until explicit approval.
    6. Approving and applying the candidate produces a passing repository test.
    """
    cli_path = find_antigravity_cli()
    if cli_path is None:
        pytest.skip("Antigravity host CLI not found on machine.")

    repo = tmp_path / "toy_real_repo"
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

    # 1. Run recovery through Antigravity host runtime
    cfg = ArgusConfig(
        provider="antigravity",
        model="gemini-2.5-pro",
        max_attempts=2,
    )
    controller = RecoveryController(config=cfg)

    outcome = controller.run(
        target_file=target_file,
        test_command=f"pytest {test_file.name}",
        test_file=test_file,
        workspace_root=repo,
    )

    if outcome.status != "SUCCESS" and "project_id is required" in str(outcome.message):
        pytest.skip("Antigravity host CLI requires an active IDE project session (project_id); headless CLI recovery is unverified in this environment.")

    # 2. Check recovery outcome
    assert outcome.status == "SUCCESS", f"Recovery did not succeed: {outcome.message}"
    assert outcome.verified is True
    assert outcome.effort_used in ("low", "medium", "high")
    assert bool(outcome.unified_diff)

    # 3. Check original file is untouched before approval
    original_code = target_file.read_text(encoding="utf-8")
    assert "return a + b" in original_code, "Original file was modified before human approval!"

    # 4. Check unapproved apply is rejected
    run_id = outcome.run_id
    rejected = AuthoritativePatchApplier.apply(
        run_id=run_id,
        workspace_root=repo,
        require_approval=True,
    )
    assert rejected.success is False
    assert rejected.error_code == "CANDIDATE_NOT_APPROVED"

    # 5. Approve candidate
    approval_record = CandidateArtifactStore.set_approval(run_id=run_id, approved=True, workspace_root=repo)
    assert approval_record.approved is True

    # 6. Apply approved candidate to source tree
    applied = AuthoritativePatchApplier.apply(
        run_id=run_id,
        workspace_root=repo,
        require_approval=True,
    )
    assert applied.success is True

    # 7. Verify the final applied fix passes tests
    final_code = target_file.read_text(encoding="utf-8")
    assert "return a * b" in final_code
    from argus.runtime.runner import SubprocessTestRunner
    runner = SubprocessTestRunner()
    final_test_res = runner.run_test(f"pytest {test_file.name}", cwd=repo)
    assert final_test_res.passed is True
