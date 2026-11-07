"""End-to-End Functional Test for Argus Recovery Engine, Verification, and Approval."""

import tempfile
from pathlib import Path
import pytest

from argus.config import ArgusConfig
from argus.providers.models import ProviderConfig
from argus.providers.adapter import OpenAICompatibleAdapter
from argus.providers.mock_server import MockOpenAIServer
from argus.runtime.candidate import AuthoritativePatchApplier, CandidateArtifactStore
from argus.runtime.controller import RecoveryController, RecoveryOutcome


def test_end_to_end_recovery_and_approval_flow():
    """Verify complete end-to-end recovery: failing test -> model repair -> verification -> approval -> apply."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        ws_root = Path(tmp_dir).resolve()
        
        # 1. Create failing target file and test
        src_dir = ws_root / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        target_file = src_dir / "calculator.py"
        target_file.write_text(
            "def add(a: int, b: int) -> int:\n"
            "    # Buggy implementation\n"
            "    return a - b\n",
            encoding="utf-8",
        )

        tests_dir = ws_root / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        test_file = tests_dir / "test_calculator.py"
        test_file.write_text(
            "from src.calculator import add\n\n"
            "def test_add():\n"
            "    assert add(2, 3) == 5\n",
            encoding="utf-8",
        )

        # 2. Launch offline mock provider
        with MockOpenAIServer() as server:
            # Model response providing SEARCH/REPLACE repair block
            repair_patch = (
                "Here is the fix for the failing test:\n\n"
                "<<<<<<< SEARCH\n"
                "    # Buggy implementation\n"
                "    return a - b\n"
                "=======\n"
                "    # Correct implementation\n"
                "    return a + b\n"
                ">>>>>>> REPLACE\n"
            )
            server.canned_response = repair_patch

            provider_cfg = ProviderConfig(
                name="offline-mock",
                base_url=server.base_url,
                model="mock-model",
                api_key="mock-key",
            )
            adapter = OpenAICompatibleAdapter(provider_cfg)

            config = ArgusConfig(
                max_attempts=3,
                provider_config=provider_cfg,
            )
            controller = RecoveryController(config=config, adapter=adapter)

            # 3. Execute recovery loop
            # Note: test command runs pytest against the test file in the isolated workspace
            test_cmd = f"pytest {test_file.relative_to(ws_root)}"
            outcome: RecoveryOutcome = controller.run(
                target_file=target_file,
                test_command=test_cmd,
                test_file=test_file,
                workspace_root=ws_root,
            )

            # 4. Assertions on outcome
            assert outcome.status == "SUCCESS"
            assert outcome.verified is True
            assert outcome.total_attempts == 1
            assert outcome.run_id != ""
            assert len(outcome.changed_files) == 1
            assert "return a + b" in outcome.patched_content
            assert "return a + b" in outcome.unified_diff

            # 5. Verify original file is UNTOUCHED
            original_content = target_file.read_text(encoding="utf-8")
            assert "return a - b" in original_content
            assert "return a + b" not in original_content

            # 6. Verify candidate stored in .argus/runs/<run_id>/
            candidates = CandidateArtifactStore.list_candidates(workspace_root=ws_root)
            assert len(candidates) >= 1
            latest = candidates[0]
            assert latest.run_id == outcome.run_id
            assert latest.verified is True
            assert latest.approved is False
            assert latest.applied is False

            diff_text = CandidateArtifactStore.get_candidate_diff(outcome.run_id, workspace_root=ws_root)
            assert "return a + b" in diff_text

            # 7. Unapproved application is REJECTED when require_approval=True (MCP enforcement)
            rejected_res = AuthoritativePatchApplier.apply(
                run_id=outcome.run_id,
                workspace_root=ws_root,
                expected_target=target_file,
                require_approval=True,
            )
            assert rejected_res.success is False
            assert rejected_res.error_code == "CANDIDATE_NOT_APPROVED"

            # Original file STILL untouched
            assert "return a - b" in target_file.read_text(encoding="utf-8")

            # 8. User grants explicit human approval
            approved_meta = CandidateArtifactStore.set_approval(outcome.run_id, approved=True, workspace_root=ws_root)
            assert approved_meta.approved is True
            assert approved_meta.approved_at is not None

            # 9. Now application SUCCEEDS
            applied_res = AuthoritativePatchApplier.apply(
                run_id=outcome.run_id,
                workspace_root=ws_root,
                expected_target=target_file,
                require_approval=True,
            )
            assert applied_res.success is True

            # 10. Original file is now patched!
            updated_content = target_file.read_text(encoding="utf-8")
            assert "return a + b" in updated_content
            assert "return a - b" not in updated_content
