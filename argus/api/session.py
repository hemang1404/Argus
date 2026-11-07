"""Session Management, Spend Telemetry, and Presets API Router.

Provides endpoints for:
- Retrieving past task execution history (/api/v1/session/history)
- Inspecting real-time USD inference spend and budget limits (/api/v1/session/spend)
- Listing and exporting client tool presets (/api/v1/session/presets)
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from argus.adapters.presets import export_preset, get_preset, list_presets
from argus.api.auth import require_session_token, validate_path_within_workspace
from argus.gateway.budget import BudgetGuard
from argus.gateway.config import BudgetConfig

logger = logging.getLogger("argus.api.session")

router = APIRouter(prefix="/api/v1/session", tags=["session"])

# Global session spend guard
_SESSION_BUDGET_GUARD = BudgetGuard(BudgetConfig(per_task_usd=0.05, per_session_usd=1.00))


def get_budget_guard() -> BudgetGuard:
    return _SESSION_BUDGET_GUARD


class ExportPresetRequest(BaseModel):
    preset_key: str = Field(..., description="Key of the client preset (e.g. 'aider', 'opencode').")
    overwrite: bool = Field(False, description="Whether to overwrite existing configuration file.")


class HistoryManager:
    """Manages appending and reading execution records from .argus/history.json."""

    @staticmethod
    def record_run(workspace: Path | str, run_record: dict) -> None:
        ws = Path(workspace).resolve()
        dot_argus = ws / ".argus"
        dot_argus.mkdir(parents=True, exist_ok=True)
        history_file = dot_argus / "history.json"

        history: List[dict] = []
        if history_file.exists():
            try:
                history = json.loads(history_file.read_text(encoding="utf-8"))
            except Exception:
                history = []

        history.append(run_record)
        history_file.write_text(json.dumps(history, indent=2), encoding="utf-8")

    @staticmethod
    def get_history(workspace: Path | str) -> List[dict]:
        ws = Path(workspace).resolve()
        history_file = ws / ".argus" / "history.json"
        if not history_file.exists():
            return []
        try:
            return json.loads(history_file.read_text(encoding="utf-8"))
        except Exception:
            return []


@router.get("/history")
async def get_session_history(
    request: Request,
    token: str = Depends(require_session_token),
) -> dict:
    """Return historical recovery runs executed in current workspace."""
    workspace = getattr(request.app.state, "workspace", Path(".").resolve())
    runs = HistoryManager.get_history(workspace)
    return {"workspace": str(workspace), "history": runs}


@router.get("/spend")
async def get_session_spend(
    token: str = Depends(require_session_token),
    guard: BudgetGuard = Depends(get_budget_guard),
) -> dict:
    """Return current session and task spend in USD."""
    return {
        "session_spend_usd": guard.get_session_spend(),
        "session_budget_limit_usd": guard.config.per_session_usd,
        "task_budget_limit_usd": guard.config.per_task_usd,
    }


@router.get("/presets")
async def get_tool_presets(
    token: str = Depends(require_session_token),
) -> dict:
    """List available external tool presets (Aider, Claude Code, OpenCode, Continue, Cursor)."""
    presets = []
    for p in list_presets():
        presets.append({
            "key": p.key,
            "display_name": p.display_name,
            "filename": p.filename,
            "description": p.description,
            "cli_command": p.cli_command,
            "instructions": p.instructions,
        })
    return {"presets": presets}


@router.post("/presets/export")
async def export_tool_preset(
    req: ExportPresetRequest,
    request: Request,
    token: str = Depends(require_session_token),
) -> dict:
    """Export tool configuration preset file into workspace."""
    workspace = getattr(request.app.state, "workspace", Path(".").resolve())
    preset = get_preset(req.preset_key)
    if not preset:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Preset '{req.preset_key}' not found.",
        )

    try:
        exported_path = export_preset(preset.key, target_dir=str(workspace), overwrite=req.overwrite)
        return {
            "status": "exported",
            "preset_key": preset.key,
            "file_path": str(exported_path),
            "display_name": preset.display_name,
        }
    except FileExistsError as ex:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(ex),
        )
    except Exception as ex:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to export preset: {str(ex)}",
        )
