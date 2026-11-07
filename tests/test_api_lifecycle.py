"""Tests for Phase 1: Local API Security, Port Discovery, and Lifecycle."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from argus.api.auth import (
    SessionTokenManager,
    validate_host_header,
    validate_path_within_workspace,
)
from argus.api.lifecycle import HeartbeatWatchdog, SessionFileManager, find_free_port
from argus.api.server import create_app


def test_find_free_port():
    """Verify that find_free_port discovers an available TCP port."""
    port = find_free_port(preferred_port=8000)
    assert isinstance(port, int)
    assert 1024 <= port <= 65535


def test_session_token_manager():
    """Verify constant-time token verification and generation."""
    manager = SessionTokenManager(token="test-secret-token-12345")
    assert manager.token == "test-secret-token-12345"
    assert manager.verify_token("test-secret-token-12345") is True
    assert manager.verify_token("wrong-token") is False
    assert manager.verify_token("") is False
    assert manager.verify_token(None) is False


def test_validate_path_within_workspace():
    """Verify path traversal prevention."""
    with tempfile.TemporaryDirectory() as tmp_root:
        root = Path(tmp_root).resolve()
        sub_file = root / "src" / "code.py"
        sub_file.parent.mkdir(parents=True, exist_ok=True)
        sub_file.write_text("print('hello')", encoding="utf-8")

        # Valid relative and absolute paths
        assert validate_path_within_workspace("src/code.py", root) == sub_file
        assert validate_path_within_workspace(sub_file, root) == sub_file

        # Directory traversal attempt
        with pytest.raises(HTTPException) as exc_info:
            validate_path_within_workspace("../../windows/system32/cmd.exe", root)
        assert exc_info.value.status_code == 403


def test_api_health_endpoint():
    """Verify that /api/v1/health is reachable unauthenticated."""
    app = create_app(workspace=".")
    client = TestClient(app)
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "version" in data
    assert "workspace" in data


def test_api_session_auth_enforcement():
    """Verify that state-changing routes require Bearer token."""
    test_token = "secret-token-abc"
    app = create_app(workspace=".", token=test_token)
    client = TestClient(app)

    # 1. No token -> 401
    resp = client.post("/api/v1/session/heartbeat")
    assert resp.status_code == 401

    # 2. Invalid token -> 401
    resp = client.post(
        "/api/v1/session/heartbeat",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401

    # 3. Valid Bearer token -> 200
    resp = client.post(
        "/api/v1/session/heartbeat",
        headers={"Authorization": f"Bearer {test_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "alive"


def test_api_host_header_validation():
    """Verify that requests with non-loopback Host header are rejected."""
    app = create_app(workspace=".")
    client = TestClient(app)

    # Host = evil.com -> 400
    resp = client.get("/api/v1/health", headers={"Host": "evil.com"})
    assert resp.status_code == 400
    assert "rejected" in resp.json()["detail"]


def test_session_file_manager():
    """Verify writing and cleaning up .argus/session.json."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        manager = SessionFileManager(workspace=tmp_dir)
        session_file = manager.write_session(port=8042, pid=1234, token_preview="secret123")
        assert session_file.exists()

        content = session_file.read_text(encoding="utf-8")
        assert "8042" in content
        assert "1234" in content

        manager.cleanup()
        assert not session_file.exists()
