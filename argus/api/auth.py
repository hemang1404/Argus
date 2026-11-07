"""Security & Authentication module for Argus Local API.

Implements ADR-003:
- Ephemeral cryptographically random Bearer tokens (secrets.token_urlsafe(32)).
- Constant-time token verification (hmac.compare_digest).
- Host header verification preventing DNS rebinding.
- Canonical path traversal prevention.
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

security_bearer = HTTPBearer(auto_error=False)


class SessionTokenManager:
    """Manages ephemeral per-instance authentication tokens."""

    def __init__(self, token: Optional[str] = None) -> None:
        if token:
            self._token = token
        else:
            env_token = os.environ.get("ARGUS_SESSION_TOKEN")
            self._token = env_token if env_token else secrets.token_urlsafe(32)

    @property
    def token(self) -> str:
        """Return the active session secret token."""
        return self._token

    def verify_token(self, provided_token: Optional[str]) -> bool:
        """Verify token using constant-time comparison to prevent timing attacks."""
        if not provided_token:
            return False
        return hmac.compare_digest(self._token, provided_token.strip())


_DEFAULT_TOKEN_MANAGER: Optional[SessionTokenManager] = None


def get_token_manager() -> SessionTokenManager:
    """Retrieve global singleton token manager."""
    global _DEFAULT_TOKEN_MANAGER
    if _DEFAULT_TOKEN_MANAGER is None:
        _DEFAULT_TOKEN_MANAGER = SessionTokenManager()
    return _DEFAULT_TOKEN_MANAGER


def set_token_manager(manager: SessionTokenManager) -> None:
    """Explicitly inject a token manager."""
    global _DEFAULT_TOKEN_MANAGER
    _DEFAULT_TOKEN_MANAGER = manager


def require_session_token(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer),
) -> str:
    """FastAPI dependency: Enforces Authorization: Bearer <TOKEN>."""
    manager = get_token_manager()
    token = None
    if credentials:
        token = credentials.credentials
    elif "x-argus-token" in request.headers:
        token = request.headers.get("x-argus-token")

    if not token or not manager.verify_token(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Missing or invalid Argus session Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


def validate_host_header(request: Request) -> None:
    """Prevent DNS rebinding by requiring Host header to match loopback."""
    host = request.headers.get("host", "").split(":")[0].strip().lower()
    allowed_hosts = {"127.0.0.1", "localhost", "::1", "testserver"}
    if host not in allowed_hosts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Host '{host}' is rejected. Argus requires loopback Host (127.0.0.1 or localhost).",
        )


def validate_path_within_workspace(
    target_path: str | Path,
    workspace_root: str | Path,
) -> Path:
    """Resolve target path and verify that it resides strictly within workspace_root.
    
    Raises:
        HTTPException(403): If the path attempts directory traversal outside the workspace.
    """
    root = Path(workspace_root).resolve()
    target = Path(target_path)
    
    # If target is relative, resolve relative to root
    resolved = (root / target).resolve() if not target.is_absolute() else target.resolve()
    
    try:
        resolved.relative_to(root)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Path traversal detected: Path '{resolved}' escapes workspace root '{root}'.",
        )
    return resolved
