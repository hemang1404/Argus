"""End-to-End Toy Repository Recovery Test with Real Provider Inference, Feedback, Approval, and Application."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from click.testing import CliRunner
import pytest

from argus.cli import main
from argus.providers.mock_server import MockOpenAIServer
from argus.runtime.candidate import CandidateArtifactStore


def test_complete_toy_repo_recovery_workflow():
    """Verify full end-to-end recovery workflow on an actual toy repository:
    1. Toy repo created with failing test.
    2. Real provider inference (MockOpenAIServer).
    3. Multi-turn feedback: Attempt 1 fails, Attempt 2 succeeds based on test failure diagnostics.
    4. Isolated workspace execution: original repo remains unmodified during repair turns.
    5. Candidate inspection and diff review via CLI.
    6. Attempting apply without approval is rejected.
    7. Explicit human approval via CLI.
    8. Authoritative application to real working tree.
    9. Passing final test in original working tree.
    """
    runner = CliRunner()

    with tempfile.TemporaryDirectory() as tmp_dir:
        toy_repo = Path(tmp_dir).resolve()
        config_dir = toy_repo / ".argus_cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        os.environ["ARGUS_CONFIG_DIR"] = str(config_dir)

        # 1. Create toy repository structure
        src_dir = toy_repo / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        tests_dir = toy_repo / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)

        target_file = src_dir / "calc.py"
        target_file.write_text(
            "def add(a: int, b: int) -> int:\n"
            "    return a - b  # Intentional bug: subtraction instead of addition\n",
            encoding="utf-8",
        )

        test_file = tests_dir / "test_calc.py"
        test_file.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).parent.parent))\n"
            "from src.calc import add\n\n"
            "def test_add():\n"
            "    assert add(2, 3) == 5\n",
            encoding="utf-8",
        )

        # 2. Verify initial test fails in toy repo
        init_run = subprocess.run(
            [sys.executable, "-m", "pytest", str(test_file)],
            cwd=str(toy_repo),
            capture_output=True,
            text=True,
        )
        assert init_run.returncode != 0
        assert "assert -1 == 5" in init_run.stdout or "FAILED" in init_run.stdout

        # 3. Setup MockOpenAIServer with multi-turn responses:
        # Turn 1: model returns incorrect patch (multiplication) -> test fails in isolated sandbox
        # Turn 2: model inspects feedback and returns correct patch (addition) -> test passes!
        turn1_patch = (
            "Here is the fix:\n"
            "```python\n"
            "<<<<<<< SEARCH\n"
            "    return a - b  # Intentional bug: subtraction instead of addition\n"
            "=======\n"
            "    return a * b\n"
            ">>>>>>> REPLACE\n"
            "```"
        )
        turn2_patch = (
            "Fix based on test failure:\n"
            "```python\n"
            "<<<<<<< SEARCH\n"
            "    return a - b  # Intentional bug: subtraction instead of addition\n"
            "=======\n"
            "    return a + b\n"
            ">>>>>>> REPLACE\n"
            "```"
        )

        with MockOpenAIServer(responses=[turn1_patch, turn2_patch]) as server:
            # 4. Configure provider in Argus
            set_res = runner.invoke(main, [
                "provider", "set",
                "--name", "toy-mock-provider",
                "--base-url", server.base_url,
                "--model", "mock-repair-model",
                "--api-key", "test-secret-key-12345",
            ])
            assert set_res.exit_code == 0
            assert "Provider 'toy-mock-provider' Saved" in set_res.output

            # 5. Run Argus recovery engine against the failing test
            run_res = runner.invoke(main, [
                "run",
                "--file", str(target_file),
                "--test", f"{sys.executable} -m pytest {test_file}",
                "--workspace", str(toy_repo),
                "--attempts", "3",
            ])
            assert run_res.exit_code == 0
            assert "Verified passing" in run_res.output
            assert "2 attempts" in run_res.output

            # 6. Verify isolation invariant: original file MUST NOT have been modified during recovery!
            assert "return a - b" in target_file.read_text(encoding="utf-8")

            # Verify candidate was recorded
            candidates = CandidateArtifactStore.list_candidates(workspace_root=toy_repo)
            assert len(candidates) >= 1
            cand = candidates[0]
            assert cand.verified is True
            assert cand.approved is False
            assert cand.attempts == 2
            run_id = cand.run_id

            # 7. Candidate inspection
            inspect_res = runner.invoke(main, ["inspect", run_id, "-w", str(toy_repo)])
            assert inspect_res.exit_code == 0
            assert run_id in inspect_res.output
            assert "verified" in inspect_res.output.lower()

            # 8. Candidate diff review
            diff_res = runner.invoke(main, ["diff", run_id, "-w", str(toy_repo)])
            assert diff_res.exit_code == 0
            assert "-    return a - b" in diff_res.output
            assert "+    return a + b" in diff_res.output

            # 9. Invariant: applying unapproved candidate is rejected
            unapproved_apply = runner.invoke(main, ["apply", "--run-id", run_id, "-w", str(toy_repo)])
            assert unapproved_apply.exit_code == 1
            assert "CANDIDATE_NOT_APPROVED" in unapproved_apply.output
            # Original file still unchanged
            assert "return a - b" in target_file.read_text(encoding="utf-8")

            # 10. Explicit human approval via CLI
            approve_res = runner.invoke(main, ["approve", run_id, "-w", str(toy_repo), "-y"])
            assert approve_res.exit_code == 0
            assert "[APPROVED]" in approve_res.output

            # 11. Authoritative application to real working tree
            apply_res = runner.invoke(main, ["apply", "--run-id", run_id, "-w", str(toy_repo)])
            assert apply_res.exit_code == 0
            assert "[APPLIED]" in apply_res.output

            # 12. Verify file on disk is updated with the fix
            updated_content = target_file.read_text(encoding="utf-8")
            assert "return a + b" in updated_content
            assert "return a - b" not in updated_content

            # 13. Final test in original repository now PASSES!
            final_run = subprocess.run(
                [sys.executable, "-m", "pytest", str(test_file)],
                cwd=str(toy_repo),
                capture_output=True,
                text=True,
            )
            assert final_run.returncode == 0
            assert "1 passed" in final_run.stdout


@pytest.mark.live_provider
def test_live_provider_toy_repo_recovery(tmp_path: Path):
    """Perform real-provider recovery on a disposable toy repo using active configured provider."""
    if os.getenv("ARGUS_RUN_LIVE_TESTS", "").strip().lower() not in ("1", "true", "yes"):
        pytest.skip("Live provider tests are disabled by default. Set ARGUS_RUN_LIVE_TESTS=1 to run.")

    from argus.config import ArgusConfig
    from argus.providers.storage import ProviderStorage
    from argus.runtime.controller import RecoveryController
    from argus.runtime.candidate import AuthoritativePatchApplier

    storage = ProviderStorage()
    active_cfg = storage.get_active_provider()
    if not active_cfg or not active_cfg.api_key:
        pytest.skip("No live provider API key configured for live recovery test.")

    toy_repo = tmp_path / "toy_repo"
    toy_repo.mkdir()
    src_file = toy_repo / "calc.py"
    test_file = toy_repo / "test_calc.py"
    src_file.write_text("def add(a: int, b: int) -> int:\n    return a - b\n", encoding="utf-8")
    test_file.write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")

    # 1. Verify initial test failure
    init_run = subprocess.run([sys.executable, "-m", "pytest", str(test_file)], cwd=str(toy_repo), capture_output=True, text=True)
    assert init_run.returncode != 0

    # 2. Run real provider recovery loop
    cfg = ArgusConfig(max_attempts=3, budget_limit_usd=0.05, provider_config=active_cfg)
    controller = RecoveryController(config=cfg)
    outcome = controller.run(
        target_file="calc.py",
        test_command=f"{sys.executable} -m pytest test_calc.py",
        test_file="test_calc.py",
        workspace_root=toy_repo,
    )
    assert outcome.verified is True
    assert outcome.status == "SUCCESS"

    # 3. Verify source file is UNCHANGED until approval and application
    assert "return a - b" in src_file.read_text(encoding="utf-8")

    # 4. Attempt unapproved application is rejected
    unapproved_res = AuthoritativePatchApplier.apply(run_id=outcome.run_id, workspace_root=toy_repo, require_approval=True)
    assert unapproved_res.success is False
    assert unapproved_res.error_code == "CANDIDATE_NOT_APPROVED"

    # 5. Explicit human review and approval
    CandidateArtifactStore.set_approval(outcome.run_id, approved=True, workspace_root=toy_repo)
    meta, _ = CandidateArtifactStore.load_candidate(outcome.run_id, workspace_root=toy_repo)
    assert meta.approved is True

    # 6. Authoritative application
    apply_res = AuthoritativePatchApplier.apply(run_id=outcome.run_id, workspace_root=toy_repo, require_approval=True)
    assert apply_res.success is True

    # 7. Verify source file is patched
    assert "return a + b" in src_file.read_text(encoding="utf-8")

    # 8. Final test in toy repository now passes
    final_run = subprocess.run([sys.executable, "-m", "pytest", str(test_file)], cwd=str(toy_repo), capture_output=True, text=True)
    assert final_run.returncode == 0
    assert "1 passed" in final_run.stdout
