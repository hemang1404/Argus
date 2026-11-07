"""Adversarial security tests for end-to-end secret and sensitive-data redaction.

Verifies:
1. Regex pattern scrubbing for synthetic API keys, tokens, passwords, and private keys.
2. Subprocess stdout/stderr and traceback scrubbing in SubprocessTestRunner.
3. Prompt construction scrubbing in RecoveryController.
4. Artifact persistence scrubbing on disk (.argus/runs/<run_id>/prompt.txt).
5. Outgoing HTTP and host CLI message payload scrubbing in ProviderAdapters.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from argus.config import ArgusConfig
from argus.providers.adapter import OpenAICompatibleAdapter
from argus.providers.antigravity import AntigravityHostAdapter
from argus.providers.models import ProviderConfig
from argus.runtime.candidate import CandidateArtifactStore
from argus.runtime.controller import RecoveryController
from argus.runtime.runner import SubprocessTestRunner
from argus.utils.redaction import scrub_object, scrub_secrets


# Synthetic, non-real test credentials for testing redaction mechanisms
SYNTHETIC_OPENAI_KEY = "sk-proj-DEMO1234567890abcdefghijklmnopqrstuvwxyz"
SYNTHETIC_ANTHROPIC_KEY = "sk-ant-api03-DEMO1234567890abcdefghijklmnopqrstuvwxyz"
SYNTHETIC_GITHUB_PAT = "ghp_DEMO1234567890abcdefghijklmnopqrstuvwxyz1234"
SYNTHETIC_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
SYNTHETIC_GOOGLE_KEY = "AIzaSyDEMO1234567890abcdefghijklmnopqr"
SYNTHETIC_BEARER_TOKEN = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy_token_payload"
SYNTHETIC_DB_URI = "postgres://admin_user:secret_password_123@db.example.com:5432/production"
SYNTHETIC_PEM_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA0syntheticKeyPayloadForTestingOnlyNotRealKey12345\n"
    "-----END RSA PRIVATE KEY-----"
)


def test_scrub_secrets_comprehensive_synthetic_tokens():
    """Verify that all synthetic token patterns are redacted from arbitrary text."""
    raw_text = (
        f"Error occurred with OpenAI key {SYNTHETIC_OPENAI_KEY} and Anthropic {SYNTHETIC_ANTHROPIC_KEY}. "
        f"GitHub token was {SYNTHETIC_GITHUB_PAT}, AWS is {SYNTHETIC_AWS_KEY}, "
        f"and Google key is {SYNTHETIC_GOOGLE_KEY}. Auth header: {SYNTHETIC_BEARER_TOKEN}. "
        f"Connected to database at {SYNTHETIC_DB_URI}."
    )
    scrubbed = scrub_secrets(raw_text)

    assert SYNTHETIC_OPENAI_KEY not in scrubbed
    assert SYNTHETIC_ANTHROPIC_KEY not in scrubbed
    assert SYNTHETIC_GITHUB_PAT not in scrubbed
    assert SYNTHETIC_AWS_KEY not in scrubbed
    assert SYNTHETIC_GOOGLE_KEY not in scrubbed
    assert "dummy_token_payload" not in scrubbed
    assert "secret_password_123" not in scrubbed

    assert "[REDACTED_SECRET]" in scrubbed
    assert "[REDACTED_PASSWORD]" in scrubbed


def test_scrub_pem_private_keys():
    """Verify that multi-line private key blocks are fully redacted."""
    raw_log = f"Server failure:\n{SYNTHETIC_PEM_KEY}\nConnection closed."
    scrubbed = scrub_secrets(raw_log)

    assert "-----BEGIN RSA PRIVATE KEY-----" not in scrubbed
    assert "syntheticKeyPayload" not in scrubbed
    assert "[REDACTED_PRIVATE_KEY]" in scrubbed


def test_scrub_object_nested_structures():
    """Verify recursive scrubbing of nested dictionaries, lists, and tuples."""
    nested = {
        "metadata": {
            "token": SYNTHETIC_OPENAI_KEY,
            "headers": [SYNTHETIC_BEARER_TOKEN, "Accept: application/json"],
        },
        "status": "ok",
    }
    cleaned = scrub_object(nested)

    assert cleaned["metadata"]["token"] == "[REDACTED_SECRET]"
    assert "dummy_token_payload" not in cleaned["metadata"]["headers"][0]
    assert cleaned["status"] == "ok"


def test_subprocess_runner_redacts_test_failure_traceback(tmp_path: Path):
    """Verify that SubprocessTestRunner redacts secrets emitted in stdout/stderr."""
    # Create a test script that outputs a synthetic token in its failure output
    test_script = tmp_path / "test_leak.py"
    test_script.write_text(
        f"import sys\n"
        f"print('Captured stdout secret: {SYNTHETIC_OPENAI_KEY}')\n"
        f"sys.stderr.write('AssertionError: expected password {SYNTHETIC_GITHUB_PAT}\\n')\n"
        f"sys.exit(1)\n",
        encoding="utf-8",
    )

    runner = SubprocessTestRunner(timeout_seconds=10)
    res = runner.run_test(f"python {test_script.name}", cwd=tmp_path)

    assert res.passed is False
    assert SYNTHETIC_OPENAI_KEY not in res.stdout
    assert SYNTHETIC_GITHUB_PAT not in res.stderr
    assert SYNTHETIC_GITHUB_PAT not in res.traceback_summary

    assert "[REDACTED_SECRET]" in res.stdout
    assert "[REDACTED_SECRET]" in res.stderr


def test_recovery_controller_build_prompt_redacts_secrets(tmp_path: Path):
    """Verify that RecoveryController._build_prompt redacts secrets in error feedback and code."""
    controller = RecoveryController(config=ArgusConfig(provider="antigravity", model="gemini-2.5-flash"))

    raw_error = (
        f"AssertionError: failed authorization token {SYNTHETIC_OPENAI_KEY}\n"
        f"Bearer token was {SYNTHETIC_BEARER_TOKEN}"
    )

    prompt = controller._build_prompt(
        file_path="src/auth.py",
        code=f"# Module with token\nTOKEN = '{SYNTHETIC_GITHUB_PAT}'\n",
        test_command="pytest tests/test_auth.py",
        error_feedback=raw_error,
        attempt=1,
    )

    assert SYNTHETIC_OPENAI_KEY not in prompt
    assert "dummy_token_payload" not in prompt
    assert SYNTHETIC_GITHUB_PAT not in prompt

    assert "[REDACTED_SECRET]" in prompt


def test_candidate_artifact_store_redacts_persisted_artifacts(tmp_path: Path):
    """Verify that CandidateArtifactStore saves scrubbed prompt and context artifacts to disk."""
    run_id = "test_redacted_run_123"
    raw_prompt = f"System prompt with leaked secret: {SYNTHETIC_OPENAI_KEY}"
    raw_response = f"Model output mentioning password {SYNTHETIC_DB_URI}"

    CandidateArtifactStore.save_run_artifact(
        run_id=run_id,
        target_file="app.py",
        test_command="pytest",
        baseline_hash="abc",
        status="SUCCESS",
        verified=True,
        tier_used=1,
        attempts=1,
        cost_usd=0.01,
        duration_ms=100.0,
        patched_content="def safe(): pass",
        unified_diff="+def safe(): pass",
        workspace_root=tmp_path,
        prompt_text=raw_prompt,
        raw_response=raw_response,
        context_window={"sample_leak": SYNTHETIC_GITHUB_PAT},
    )

    run_dir = tmp_path / ".argus" / "runs" / run_id
    prompt_file = run_dir / "prompt.txt"
    resp_file = run_dir / "raw_response.txt"
    context_file = run_dir / "context_window.json"

    assert prompt_file.is_file()
    prompt_on_disk = prompt_file.read_text(encoding="utf-8")
    assert SYNTHETIC_OPENAI_KEY not in prompt_on_disk
    assert "[REDACTED_SECRET]" in prompt_on_disk

    resp_on_disk = resp_file.read_text(encoding="utf-8")
    assert "secret_password_123" not in resp_on_disk
    assert "[REDACTED_PASSWORD]" in resp_on_disk

    context_on_disk = context_file.read_text(encoding="utf-8")
    assert SYNTHETIC_GITHUB_PAT not in context_on_disk
    assert "[REDACTED_SECRET]" in context_on_disk


def test_adapter_chat_completion_redacts_outgoing_payload():
    """Verify that OpenAICompatibleAdapter unconditionally scrubs outgoing messages."""
    cfg = ProviderConfig(
        name="openai",
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        api_key="mock_key_for_testing",
    )
    adapter = OpenAICompatibleAdapter(cfg)

    # Mock the internal client post to capture outgoing JSON payload
    mock_post = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "fixed code"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    mock_post.return_value = mock_resp

    with patch.object(adapter, "_get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.post = mock_post
        mock_get_client.return_value = mock_client

        adapter.chat_completion(
            messages=[
                {"role": "user", "content": f"Fix test with token: {SYNTHETIC_OPENAI_KEY}"}
            ]
        )

        assert mock_post.called
        call_kwargs = mock_post.call_args[1]
        sent_payload = call_kwargs["json"]
        sent_content = sent_payload["messages"][0]["content"]

        assert SYNTHETIC_OPENAI_KEY not in sent_content
        assert "[REDACTED_SECRET]" in sent_content
