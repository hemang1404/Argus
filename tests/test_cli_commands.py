"""Tests for Argus CLI Commands: Provider, Candidates, Approval, and MCP."""

import json
import tempfile
from pathlib import Path
from click.testing import CliRunner
import pytest

from argus.cli import main
from argus.providers.mock_server import MockOpenAIServer
from argus.runtime.candidate import CandidateArtifactStore


def test_cli_provider_workflow(monkeypatch: pytest.MonkeyPatch):
    """Verify provider add, list, select, test, and delete via CLI."""
    runner = CliRunner()

    with tempfile.TemporaryDirectory() as tmp_dir:
        # Override config dir to isolate test
        tmp_path = Path(tmp_dir)
        monkeypatch.setenv("ARGUS_CONFIG_DIR", str(tmp_path))

        with MockOpenAIServer() as server:
            # 1. Add custom mock provider
            res = runner.invoke(main, [
                "provider", "set",
                "--name", "test-mock",
                "--base-url", server.base_url,
                "--model", "mock-model",
                "--api-key", "test-key-999",
            ])
            assert res.exit_code == 0
            assert "Provider 'test-mock' Saved" in res.output

            # 2. List providers
            res = runner.invoke(main, ["provider", "list"])
            assert res.exit_code == 0
            assert "test-mock" in res.output
            assert "*" in res.output

            # 3. Test provider connection
            res = runner.invoke(main, ["provider", "test", "test-mock"])
            assert res.exit_code == 0
            assert "Connected Successfully" in res.output

            # 4. Switch active provider
            res = runner.invoke(main, ["provider", "select", "antigravity"])
            assert res.exit_code == 0
            assert "Active provider set to 'antigravity'" in res.output

            # 5. Delete provider
            res = runner.invoke(main, ["provider", "delete", "test-mock"])
            assert res.exit_code == 0
            assert "Provider 'test-mock' removed" in res.output


def test_cli_candidates_approval_and_inspect():
    """Verify candidate listing, inspection, approval, and rejection via CLI."""
    runner = CliRunner()

    with tempfile.TemporaryDirectory() as tmp_dir:
        ws_root = Path(tmp_dir).resolve()

        # Seed candidate run
        CandidateArtifactStore.save_run_artifact(
            run_id="run_test_cli_123",
            target_file="src/foo.py",
            test_command="pytest tests/test_foo.py",
            baseline_hash="abc",
            status="SUCCESS",
            verified=True,
            tier_used=1,
            attempts=1,
            cost_usd=0.0001,
            duration_ms=50.0,
            patched_content="def foo(): return True\n",
            unified_diff="--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1 +1 @@\n-def foo(): return False\n+def foo(): return True\n",
            workspace_root=ws_root,
        )

        # 1. List candidates
        res = runner.invoke(main, ["candidates", "-w", str(ws_root)])
        assert res.exit_code == 0
        assert "run_test_cli_123" in res.output
        assert "PENDING" in res.output

        # 2. Inspect candidate
        res = runner.invoke(main, ["inspect", "run_test_cli_123", "-w", str(ws_root)])
        assert res.exit_code == 0
        assert "src/foo.py" in res.output
        assert "def foo(): return True" in res.output

        # 3. View diff
        res = runner.invoke(main, ["diff", "run_test_cli_123", "-w", str(ws_root)])
        assert res.exit_code == 0
        assert "+def foo(): return True" in res.output

        # 4a. Approve candidate: cancelled when user says no
        res_cancel = runner.invoke(main, ["approve", "run_test_cli_123", "-w", str(ws_root)], input="n\n")
        assert "Approval cancelled by user" in res_cancel.output
        candidates = CandidateArtifactStore.list_candidates(workspace_root=ws_root)
        assert candidates[0].approved is False

        # 4b. Approve candidate: confirmed with diff display
        res = runner.invoke(main, ["approve", "run_test_cli_123", "-w", str(ws_root), "-y"])
        assert res.exit_code == 0
        assert "Proposed Patch Diff:" in res.output
        assert "+def foo(): return True" in res.output
        assert "[APPROVED]" in res.output

        # Verify candidate is now approved
        candidates = CandidateArtifactStore.list_candidates(workspace_root=ws_root)
        assert candidates[0].approved is True

        # 5. Reject candidate
        res = runner.invoke(main, ["reject", "run_test_cli_123", "-w", str(ws_root)])
        assert res.exit_code == 0
        assert "[REJECTED]" in res.output

        # Verify candidate approval is revoked
        candidates = CandidateArtifactStore.list_candidates(workspace_root=ws_root)
        assert candidates[0].approved is False


def test_cli_mcp_commands():
    """Verify mcp verify and install commands via CLI."""
    runner = CliRunner()

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        # 1. MCP install to workspace
        res = runner.invoke(main, [
            "mcp", "install", "all",
            "--workspace",
            "--workspace-path", str(tmp_path),
        ])
        assert res.exit_code == 0
        assert "MCP server entries successfully configured" in res.output

        # 2. MCP verify
        res = runner.invoke(main, ["mcp", "verify"])
        assert res.exit_code == 0
        assert "MCP Server Verified" in res.output
        assert "argus_status" in res.output


def test_cli_provider_interactive_prompt_and_secret_redaction(monkeypatch: pytest.MonkeyPatch):
    """Verify that omitting --api-key triggers interactive hidden input and redaction."""
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmp_dir:
        import os
        from unittest.mock import patch
        monkeypatch.setenv("ARGUS_CONFIG_DIR", str(tmp_dir))

        # 1. Interactive hidden prompt when --api-key is omitted
        secret_key = "sk-super-secret-123456789"
        res = runner.invoke(
            main,
            ["provider", "set", "--preset", "openai"],
            input=f"{secret_key}\n",
        )
        assert res.exit_code == 0
        assert "Provider 'openai' Saved" in res.output
        # Invariant: Secret key is NOT printed in plaintext in output
        assert secret_key not in res.output
        assert "sk-s...6789" in res.output

        # Verify key was stored securely
        from argus.providers.storage import ProviderStorage
        storage = ProviderStorage()
        saved = storage.get_provider("openai")
        assert saved is not None
        assert saved.api_key == secret_key

        # 2. Re-running and pressing Enter preserves existing key
        res2 = runner.invoke(
            main,
            ["provider", "set", "--preset", "openai", "--model", "gpt-4o"],
            input="\n",  # empty input -> keep existing
        )
        assert res2.exit_code == 0
        assert "gpt-4o" in res2.output
        assert secret_key not in res2.output

        saved2 = storage.get_provider("openai")
        assert saved2.model == "gpt-4o"
        assert saved2.api_key == secret_key
