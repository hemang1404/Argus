"""Argus Local Desktop & Management API Server.

Provides endpoints for:
- Healthcheck and readiness probing (/api/v1/health)
- Session lifecycle and heartbeat monitoring (/api/v1/session/heartbeat, /api/v1/session/exit)
- Recovery loop execution and SSE streaming (added in Phase 3)
- Unified diff review and patch application (added in Phase 4)
- Presets and spend telemetry (added in Phase 5)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from argus import __version__
from argus.api.auth import (
    SessionTokenManager,
    require_session_token,
    set_token_manager,
    validate_host_header,
)
from argus.api.lifecycle import HeartbeatWatchdog, SessionFileManager

logger = logging.getLogger("argus.api.server")


def create_app(
    workspace: str = ".",
    token: Optional[str] = None,
    watchdog: Optional[HeartbeatWatchdog] = None,
) -> FastAPI:
    """Create and configure the FastAPI application for the Argus Local API."""
    app = FastAPI(
        title="Argus Local API",
        version=__version__,
        description="Localhost-first execution and recovery management API for Argus.",
    )

    workspace_path = Path(workspace).resolve()
    token_manager = SessionTokenManager(token=token)
    set_token_manager(token_manager)

    # Store state on app
    app.state.workspace = workspace_path
    app.state.token_manager = token_manager
    app.state.watchdog = watchdog
    app.state.session_file_manager = SessionFileManager(workspace_path)

    # 1. CORS Configuration (Strict local origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1",
            "http://localhost",
            "http://127.0.0.1:8000",
            "http://localhost:8000",
        ],
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # 2. Host Validation Middleware
    @app.middleware("http")
    async def host_validation_middleware(request: Request, call_next):
        try:
            validate_host_header(request)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(request)

    # 3. Base Health & Diagnostics (Unauthenticated for readiness probe)
    @app.get("/api/v1/health")
    async def health_check() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "workspace": str(workspace_path),
            "pid": os.getpid(),
        }

    # 4. Session Heartbeat (Requires Token)
    @app.post("/api/v1/session/heartbeat")
    async def session_heartbeat(
        token: str = Depends(require_session_token),
    ) -> dict:
        if app.state.watchdog:
            app.state.watchdog.record_heartbeat()
        return {"status": "alive", "timestamp": sys.float_info.max}

    # 5. Session Exit (Requires Token)
    @app.post("/api/v1/session/exit")
    async def session_exit(
        token: str = Depends(require_session_token),
    ) -> dict:
        logger.info("Session exit requested by authorized client.")
        if app.state.session_file_manager:
            app.state.session_file_manager.cleanup()
        
        # Asynchronously schedule immediate process termination
        loop = asyncio.get_event_loop()
        loop.call_later(0.1, lambda: os._exit(0))
        return {"status": "shutting_down"}

    # 6. Include API Routers (Phases 3 & 5)
    from argus.api.recovery import router as recovery_router
    from argus.api.session import router as session_router
    app.include_router(recovery_router)
    app.include_router(session_router)

    # 7. Mount Static UI assets if directory exists
    ui_dir = Path(__file__).parent.parent / "ui"
    if ui_dir.exists() and (ui_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="ui")

    return app
