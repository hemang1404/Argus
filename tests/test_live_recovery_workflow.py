"""End-to-End Live Recovery Validation Tests.

Validates the genuine end-to-end coding recovery workflow using real authorized Antigravity host inference:
1. Real client request forwarding and streaming through Argus Gateway.
2. Verified automated test recovery executed in a temporary isolated Git worktree.
3. Patch validation, AST checking, and closed-loop test execution.
4. Workspace preservation invariant on failed recovery attempts.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from argus.config import ArgusConfig
from argus.gateway.config import GatewayConfig, ProviderConfig as GWProviderConfig, TierDefinition
from argus.gateway.server import app, init_gateway
from argus.gateway.telemetry import TelemetryRegistry
from argus.providers.antigravity import find_antigravity_cli
from argus.providers.models import ProviderConfig
from argus.runtime.candidate import AuthoritativePatchApplier, CandidateArtifactStore
from argus.runtime.controller import RecoveryController, RecoveryOutcome


def get_live_antigravity_config() -> ProviderConfig:
    """Retrieve verified live Antigravity host configuration."""
    if os.getenv("ARGUS_RUN_LIVE_TESTS", "").strip().lower() not in ("1", "true", "yes"):
        pytest.skip("Live Antigravity tests are opt-in. Set ARGUS_RUN_LIVE_TESTS=1 to run.")

    cli_path = find_antigravity_cli()
    if not cli_path:
        pytest.skip("Antigravity host CLI (agy) not found in system.")

    return ProviderConfig(
        name="antigravity",
        base_url="host://antigravity-cli",
        model="gemini-3.8-flash-low",
        timeout_seconds=45.0,
    )


@pytest.mark.live_antigravity
def test_live_gateway_forwarding_and_streaming(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify request forwarding, response handling, telemetry, and SSE streaming over live Antigravity."""
    host_cfg = get_live_antigravity_config()

    monkeypatch.chdir(tmp_path)
    TelemetryRegistry._instance = None
    registry = TelemetryRegistry.get(workspace_root=tmp_path)
    registry.clear()

    # Configure gateway with the live Antigravity host provider
    gw_config = GatewayConfig(
        providers=[
            GWProviderConfig(
                name="antigravity",
                base_url="host://antigravity-cli",
            ),
        ],
        tiers={
            "cheap": TierDefinition(provider="antigravity", model="gemini-3.8-flash-low"),
            "frontier": TierDefinition(provider="antigravity", model="gemini-3.8-flash-low"),
        },
    )
    init_gateway(gw_config, workspace=tmp_path)
    client = TestClient(app)

    # 1. Non-streaming live request
    resp = client.post(
        "/v1/chat/completions",
        headers={
            "User-Agent": "aider/0.30.0",
        },
        json={
            "model": "gemini-3.8-flash-low",
            "messages": [
                {"role": "user", "content": "Respond with the word 'VERIFIED' and nothing else."}
            ],
            "stream": False,
            "max_tokens": 50,
        },
    )
    assert resp.status_code == 200, f"Gateway error: {resp.text}"
    data = resp.json()
    assert "choices" in data and len(data["choices"]) > 0
    content = data["choices"][0]["message"]["content"]
    assert "VERIFIED" in content.upper()
    assert data.get("usage", {}).get("total_tokens", 0) > 0

    # 2. Streaming live request (SSE)
    stream_resp = client.post(
        "/v1/chat/completions",
        headers={
            "User-Agent": "cursor/0.1.0",
        },
        json={
            "model": "gemini-3.8-flash-low",
            "messages": [
                {"role": "user", "content": "Count from 1 to 3 separated by spaces."}
            ],
            "stream": True,
            "max_tokens": 50,
        },
    )
    assert stream_resp.status_code == 200
    assert "text/event-stream" in stream_resp.headers.get("content-type", "")

    chunks = []
    has_done = False
    for line in stream_resp.iter_lines():
        if line.startswith("data: "):
            payload = line[6:].strip()
            if payload == "[DONE]":
                has_done = True
                break
            try:
                parsed = json.loads(payload)
                delta = parsed["choices"][0]["delta"].get("content", "")
                chunks.append(delta)
            except Exception:
                pass

    assert has_done, "Streaming response did not terminate with [DONE]"
    full_streamed = "".join(chunks)
    assert any(digit in full_streamed for digit in ["1", "2", "3"])


@pytest.mark.live_antigravity
def test_live_recovery_success_in_git_worktree(tmp_path: Path):
    """Verify live closed-loop test repair in an isolated Git worktree."""
    host_cfg = get_live_antigravity_config()

    # 1. Initialize a clean temporary Git repository
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Argus Tester"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.email", "tester@argus.local"], cwd=str(repo_dir), check=True)

    # 2. Create buggy source file and test file
    src_file = repo_dir / "math_utils.py"
    src_file.write_text(
        "def sum_even_numbers(numbers):\n"
        "    \"\"\"Return the sum of all even numbers in the list.\"\"\"\n"
        "    total = 0\n"
        "    for n in numbers:\n"
        "        if n % 2 != 0:  # BUG: checks odd instead of even\n"
        "            total += n\n"
        "    return total\n",
        encoding="utf-8",
    )

    test_file = repo_dir / "test_math_utils.py"
    test_file.write_text(
        "from math_utils import sum_even_numbers\n\n"
        "def test_sum_even_numbers():\n"
        "    assert sum_even_numbers([1, 2, 3, 4, 5, 6]) == 12\n"
        "    assert sum_even_numbers([1, 3, 5]) == 0\n"
        "    assert sum_even_numbers([2, 4, 6]) == 12\n",
        encoding="utf-8",
    )

    # Commit initial state to git
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial buggy commit"], cwd=str(repo_dir), check=True, capture_output=True)

    orig_content = src_file.read_text(encoding="utf-8")
    orig_hash = hashlib.sha256(orig_content.encode("utf-8")).hexdigest()

    # 3. Configure Argus Recovery Controller with live Antigravity host provider
    config = ArgusConfig(
        provider="antigravity",
        model="gemini-3.8-flash-low",
        max_attempts=3,
        budget_limit_usd=0.05,
        test_timeout_seconds=30,
        provider_config=host_cfg,
    )
    controller = RecoveryController(config=config)

    # Run recovery inside isolated Git worktree
    outcome: RecoveryOutcome = controller.run(
        target_file=src_file,
        test_command=f"{sys.executable} -m pytest {test_file.name}",
        test_file=test_file,
        workspace_root=repo_dir,
        use_worktree=True,
    )

    # 4. Verify recovery outcome
    assert outcome.status == "SUCCESS", f"Recovery failed: {outcome.message} ({outcome.diagnostics})"
    assert outcome.verified is True
    assert outcome.total_attempts >= 1
    assert outcome.unified_diff != ""

    # 5. Verify SAFETY INVARIANT: Original repo file is 100% UNTOUCHED
    current_content = src_file.read_text(encoding="utf-8")
    current_hash = hashlib.sha256(current_content.encode("utf-8")).hexdigest()
    assert current_hash == orig_hash, "Original workspace was modified before explicit approval!"
    assert "BUG: checks odd" in current_content

    # 6. Verify candidate artifact exists in .argus/runs/<run_id>/
    metadata, _ = CandidateArtifactStore.load_candidate(outcome.run_id, workspace_root=repo_dir)
    assert metadata.verified is True
    assert metadata.approved is False
    assert metadata.applied is False

    # 7. Authoritative human approval and patch application
    CandidateArtifactStore.set_approval(outcome.run_id, approved=True, workspace_root=repo_dir)
    apply_res = AuthoritativePatchApplier.apply(run_id=outcome.run_id, workspace_root=repo_dir, require_approval=True)
    assert apply_res.success is True

    # 8. Verify the patch is now applied and tests pass in the repository
    repaired_content = src_file.read_text(encoding="utf-8")
    assert any(k in repaired_content for k in ["n % 2 == 0", "not (n % 2)", "not n % 2", "== 0", "even"])

    test_run = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file)],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )
    assert test_run.returncode == 0, f"Tests failed after applying candidate:\n{test_run.stdout}\n{test_run.stderr}"


@pytest.mark.live_antigravity
def test_live_recovery_failure_preserves_workspace(tmp_path: Path):
    """Verify that an unresolvable failure preserves the original workspace without corruption."""
    host_cfg = get_live_antigravity_config()

    # 1. Initialize temporary Git repository
    repo_dir = tmp_path / "repo_fail"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Argus Tester"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.email", "tester@argus.local"], cwd=str(repo_dir), check=True)

    # 2. Impossible / contradictory task with 1 attempt limit
    src_file = repo_dir / "unsolvable.py"
    src_file.write_text(
        "def compute(x):\n"
        "    return x\n",
        encoding="utf-8",
    )

    test_file = repo_dir / "test_unsolvable.py"
    test_file.write_text(
        "from unsolvable import compute\n\n"
        "def test_compute_impossible():\n"
        "    # Intentionally contradictory requirement: return both 1 and 2 for same input\n"
        "    assert compute(42) == 1\n"
        "    assert compute(42) == 2\n",
        encoding="utf-8",
    )

    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo_dir), check=True, capture_output=True)

    orig_content = src_file.read_text(encoding="utf-8")
    orig_hash = hashlib.sha256(orig_content.encode("utf-8")).hexdigest()

    # 3. Configure Argus with max_attempts=1
    config = ArgusConfig(
        provider="antigravity",
        model="gemini-3.8-flash-low",
        max_attempts=1,
        budget_limit_usd=0.01,
        test_timeout_seconds=20,
        provider_config=host_cfg,
    )
    controller = RecoveryController(config=config)

    outcome: RecoveryOutcome = controller.run(
        target_file=src_file,
        test_command=f"{sys.executable} -m pytest {test_file.name}",
        test_file=test_file,
        workspace_root=repo_dir,
        use_worktree=True,
    )

    # 4. Verify failed status
    assert outcome.status in ("FAILED_MAX_ATTEMPTS", "FAILED")
    assert outcome.verified is False

    # 5. Verify SAFETY INVARIANT: Original repo file is completely preserved
    post_content = src_file.read_text(encoding="utf-8")
    post_hash = hashlib.sha256(post_content.encode("utf-8")).hexdigest()
    assert post_hash == orig_hash, "Original file modified despite failed recovery!"
