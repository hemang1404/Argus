"""Acceptance tests for WP-5 (Runtime Isolation Honesty) and WP-6 (Workspace Lifecycle & Reliability).

Validates:
1. WP-5: SubprocessTestRunner never reports 'container' for host execution.
2. WP-5: SubprocessTestRunner refuses execution when container isolation is requested (fails closed).
3. WP-6: Session-aware workspace GC never deletes active workspaces owned by living PIDs.
4. WP-6: Abandoned workspaces with dead PIDs older than threshold are cleaned safely.
5. WP-6: Subprocess output capture is bounded in memory and truncates gracefully.
6. WP-6: BoundedOutputCollector preserves head and tail with explicit omission markers.
7. WP-6: SubprocessTestRunner terminates runaway processes upon timeout.
8. WP-6: OpenAICompatibleAdapter deterministically manages and closes HTTP client resources.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from argus.providers.adapter import OpenAICompatibleAdapter
from argus.providers.models import ProviderConfig
from argus.runtime.runner import BoundedOutputCollector, SubprocessTestRunner, TestExecutionResult
from argus.runtime.workspace import (
    WorkspaceManager,
    cleanup_stale_temp_workspaces,
    is_process_alive,
)


# ============================================================================
# WP-5: Runtime Isolation Honesty
# ============================================================================

def test_runner_host_execution_reports_host_isolation(tmp_path: Path) -> None:
    """Normal execution on the host must truthfully report isolation_used='host'."""
    runner = SubprocessTestRunner(timeout_seconds=5)
    res = runner.run_test("python -c \"print('hello')\"", cwd=tmp_path)
    assert res.passed is True
    assert res.isolation_used == "host"
    assert "hello" in res.stdout


def test_runner_container_mode_refuses_when_engine_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When container isolation is requested but Docker is missing, runner must fail closed."""
    from argus.runtime import runner as runner_mod
    monkeypatch.setattr(runner_mod, "is_container_available", lambda: False)

    runner = SubprocessTestRunner(isolation_mode="container")
    res = runner.run_test("python -c \"print('untrusted')\"", cwd=tmp_path)
    assert res.passed is False
    assert res.exit_code == 125
    assert res.isolation_used == "refused"
    assert "ISOLATION REFUSAL" in res.traceback_summary
    assert "no running container engine" in res.stderr


def test_runner_container_mode_refuses_even_when_engine_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """SubprocessTestRunner must never execute untrusted code on the host pretending it's in a container."""
    from argus.runtime import runner as runner_mod
    monkeypatch.setattr(runner_mod, "is_container_available", lambda: True)

    runner = SubprocessTestRunner(isolation_mode="container")
    res = runner.run_test("python -c \"print('untrusted')\"", cwd=tmp_path)
    assert res.passed is False
    assert res.exit_code == 125
    assert res.isolation_used == "refused"
    assert "ISOLATION REFUSAL" in res.traceback_summary
    assert "SubprocessTestRunner executes directly on the host" in res.stderr


def test_runner_untrusted_flag_refuses_host_execution(tmp_path: Path) -> None:
    """Passing untrusted=True to SubprocessTestRunner must refuse host execution."""
    runner = SubprocessTestRunner()
    res = runner.run_test("python -c \"print('untrusted')\"", cwd=tmp_path, untrusted=True)
    assert res.passed is False
    assert res.exit_code == 125
    assert res.isolation_used == "refused"


# ============================================================================
# WP-6: Bounded Subprocess Output & Memory Safety
# ============================================================================

def test_bounded_output_collector_small_content() -> None:
    """Output smaller than max_bytes is completely preserved without truncation markers."""
    collector = BoundedOutputCollector(max_bytes=1024, head_bytes=256)
    collector.feed(b"Line 1\nLine 2\nLine 3\n")
    text = collector.get_text()
    assert text == "Line 1\nLine 2\nLine 3\n"
    assert "TRUNCATED" not in text


def test_bounded_output_collector_large_content_preserves_head_and_tail() -> None:
    """Output exceeding max_bytes preserves head, tail, and inserts an explicit omission marker."""
    collector = BoundedOutputCollector(max_bytes=200, head_bytes=50)
    # Feed 1000 bytes
    payload = b"HEAD_DATA_" + (b"A" * 900) + b"_TAIL_DATA"
    collector.feed(payload)

    text = collector.get_text()
    assert text.startswith("HEAD_DATA_")
    assert text.endswith("_TAIL_DATA")
    assert "OUTPUT TRUNCATED" in text
    assert "omitted by Argus security limits" in text


def test_runner_bounded_output_execution(tmp_path: Path) -> None:
    """SubprocessTestRunner limits captured output and prevents memory blowout."""
    runner = SubprocessTestRunner(timeout_seconds=5, max_output_bytes=1024)
    # Print 50,000 characters
    cmd = "python -c \"print('START_' + 'X' * 50000 + '_END')\""
    res = runner.run_test(cmd, cwd=tmp_path)
    assert res.passed is True
    assert res.stdout.startswith("START_")
    assert res.stdout.strip().endswith("_END")
    assert "OUTPUT TRUNCATED" in res.stdout
    assert len(res.stdout) < 5000  # Strictly bounded


def test_runner_timeout_terminates_process(tmp_path: Path) -> None:
    """SubprocessTestRunner terminates processes exceeding the configured deadline."""
    runner = SubprocessTestRunner(timeout_seconds=1)
    res = runner.run_test("python -c \"import time; time.sleep(10)\"", cwd=tmp_path)
    assert res.passed is False
    assert res.timed_out is True
    assert "TIMEOUT" in res.traceback_summary


# ============================================================================
# WP-6: Workspace Lifecycle & Session-Aware Garbage Collection
# ============================================================================

def test_is_process_alive() -> None:
    """Current process PID must be reported as alive; invalid PID must be dead."""
    current_pid = os.getpid()
    assert is_process_alive(current_pid) is True
    assert is_process_alive(99999999) is False
    assert is_process_alive(-1) is False


def test_cleanup_stale_temp_workspaces_preserves_active_session() -> None:
    """Active workspaces owned by living processes must NOT be deleted, even if older than 1 hour."""
    temp_dir = Path(tempfile.gettempdir())
    ws_dir = temp_dir / f"argus_ws_test_active_{os.getpid()}"
    ws_dir.mkdir(parents=True, exist_ok=True)

    session_info = {
        "session_id": "test_active_session",
        "pid": os.getpid(),  # Alive!
        "created_at": time.time() - 7200,  # 2 hours old
    }
    (ws_dir / ".argus_session.json").write_text(json.dumps(session_info), encoding="utf-8")

    try:
        # Age the directory artificially to 2 hours ago
        two_hours_ago = time.time() - 7200
        os.utime(ws_dir, (two_hours_ago, two_hours_ago))

        cleaned = cleanup_stale_temp_workspaces(max_age_hours=1.0)
        # Active workspace MUST still exist!
        assert ws_dir.exists() is True
    finally:
        import shutil
        shutil.rmtree(ws_dir, ignore_errors=True)


def test_cleanup_stale_temp_workspaces_reclaims_dead_session() -> None:
    """Abandoned workspaces whose owning process is dead and older than threshold MUST be pruned."""
    temp_dir = Path(tempfile.gettempdir())
    ws_dir = temp_dir / "argus_ws_test_dead_session"
    ws_dir.mkdir(parents=True, exist_ok=True)

    session_info = {
        "session_id": "test_dead_session",
        "pid": 99999999,  # Non-existent dead PID
        "created_at": time.time() - 7200,
    }
    (ws_dir / ".argus_session.json").write_text(json.dumps(session_info), encoding="utf-8")

    try:
        two_hours_ago = time.time() - 7200
        os.utime(ws_dir, (two_hours_ago, two_hours_ago))

        cleaned = cleanup_stale_temp_workspaces(max_age_hours=1.0)
        assert ws_dir.exists() is False
    finally:
        import shutil
        shutil.rmtree(ws_dir, ignore_errors=True)


def test_workspace_manager_session_metadata_created(tmp_path: Path) -> None:
    """WorkspaceManager.setup() must create .argus_session.json with current PID and session_id."""
    target_file = tmp_path / "app.py"
    target_file.write_text("print('test')", encoding="utf-8")

    with WorkspaceManager(repo_root=tmp_path, target_file=target_file) as ws:
        assert ws.temp_dir is not None
        session_file = ws.temp_dir / ".argus_session.json"
        assert session_file.exists() is True

        data = json.loads(session_file.read_text(encoding="utf-8"))
        assert data["pid"] == os.getpid()
        assert data["session_id"] == ws.session_id


# ============================================================================
# WP-6: HTTP Client Lifecycle
# ============================================================================

def test_openai_adapter_client_lifecycle_and_reuse() -> None:
    """OpenAICompatibleAdapter reuses its managed client across calls and closes it deterministically."""
    cfg = ProviderConfig(name="test", base_url="http://localhost:1234/v1", model="test-model")
    adapter = OpenAICompatibleAdapter(config=cfg)

    client1 = adapter._get_client()
    client2 = adapter._get_client()
    assert client1 is client2
    assert client1.is_closed is False

    adapter.close()
    assert client1.is_closed is True
    assert adapter._managed_client is None


def test_openai_adapter_context_manager() -> None:
    """OpenAICompatibleAdapter used as context manager closes its client on exit."""
    cfg = ProviderConfig(name="test", base_url="http://localhost:1234/v1", model="test-model")
    with OpenAICompatibleAdapter(config=cfg) as adapter:
        client = adapter._get_client()
        assert client.is_closed is False

    assert client.is_closed is True
