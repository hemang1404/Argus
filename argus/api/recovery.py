"""Recovery Controller API Adapter & Server-Sent Events (SSE) Streaming.

Coordinates asynchronous background execution of RecoveryController,
yielding real-time execution events to desktop clients over SSE.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from argus.api.auth import (
    get_token_manager,
    require_session_token,
    validate_path_within_workspace,
)
from argus.config import ArgusConfig
from argus.runtime.candidate import (
    AuthoritativePatchApplier,
    CandidateApplicationError,
    CandidateArtifactStore,
)
from argus.runtime.controller import AttemptRecord, RecoveryController, RecoveryOutcome

logger = logging.getLogger("argus.api.recovery")

router = APIRouter(prefix="/api/v1/recovery", tags=["recovery"])


class RecoveryTaskRequest(BaseModel):
    target_file: str = Field(..., description="Relative or absolute path to target file.")
    test_command: str = Field(..., description="Command to execute test suite.")
    test_file: Optional[str] = Field(None, description="Optional path to test file.")
    model: str = Field("argus/auto", description="Model tier or model identifier.")
    provider: Optional[str] = Field(None, description="Explicit provider name, or 'auto' to request host discovery.")
    max_attempts: int = Field(4, ge=1, le=8, description="Maximum recovery attempts.")
    budget: float = Field(0.05, gt=0.0, description="Cost limit in USD.")


class TaskState:
    def __init__(self, task_id: str, request: RecoveryTaskRequest, workspace_root: Path) -> None:
        self.task_id = task_id
        self.request = request
        self.workspace_root = workspace_root
        self.event_queue: asyncio.Queue[Optional[dict]] = asyncio.Queue()
        self.outcome: Optional[RecoveryOutcome] = None
        self.is_active = True
        self.baseline_hash: str = ""


class TaskManager:
    """Manages active and completed recovery tasks."""

    def __init__(self) -> None:
        self._tasks: Dict[str, TaskState] = {}

    def create_task(self, request: RecoveryTaskRequest, workspace_root: Path) -> TaskState:
        task_id = str(uuid.uuid4())
        state = TaskState(task_id, request, workspace_root)
        self._tasks[task_id] = state
        return state

    def get_task(self, task_id: str) -> Optional[TaskState]:
        return self._tasks.get(task_id)

    def is_any_task_active(self) -> bool:
        return any(t.is_active for t in self._tasks.values())


task_manager = TaskManager()


def verify_sse_token(request: Request, token: Optional[str] = Query(None)) -> str:
    """Authenticate SSE connections via query param or Authorization header."""
    mgr = get_token_manager()
    auth_header = request.headers.get("authorization")
    extracted_token = None

    if auth_header and auth_header.lower().startswith("bearer "):
        extracted_token = auth_header[7:].strip()
    elif token:
        extracted_token = token.strip()
    elif "x-argus-token" in request.headers:
        extracted_token = request.headers["x-argus-token"].strip()

    if not extracted_token or not mgr.verify_token(extracted_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized SSE connection: Missing or invalid session token.",
        )
    return extracted_token


@router.post("/run")
async def start_recovery_task(
    req: RecoveryTaskRequest,
    request: Request,
    token: str = Depends(require_session_token),
) -> dict:
    """Dispatch non-blocking code recovery task in background thread."""
    workspace_root = getattr(request.app.state, "workspace", Path(".").resolve())
    
    # 1. Path containment check (ADR-003)
    target_path = validate_path_within_workspace(req.target_file, workspace_root)
    test_path = None
    if req.test_file:
        test_path = validate_path_within_workspace(req.test_file, workspace_root)

    # 2. Register task
    state = task_manager.create_task(req, workspace_root)
    state.baseline_hash = calculate_file_hash(target_path)

    # 3. Spawn background execution thread
    asyncio.create_task(_run_task_worker(state, target_path, test_path))

    return {
        "task_id": state.task_id,
        "status": "started",
        "target_file": str(target_path),
    }


async def _run_task_worker(
    state: TaskState,
    target_path: Path,
    test_path: Optional[Path],
) -> None:
    """Worker running synchronous RecoveryController loop on thread pool."""
    loop = asyncio.get_running_loop()

    config = ArgusConfig(
        provider=state.request.provider,
        model=state.request.model,
        max_attempts=state.request.max_attempts,
        budget_limit_usd=state.request.budget,
    )
    controller = RecoveryController(config=config)

    def on_attempt_start(attempt: int, max_attempts: int):
        event = {
            "type": "ATTEMPT_START",
            "payload": {
                "attempt": attempt,
                "max_attempts": max_attempts,
                "task_id": state.task_id,
            },
        }
        try:
            fut = asyncio.run_coroutine_threadsafe(state.event_queue.put(event), loop)
            fut.result(timeout=5.0)
        except Exception as ex:
            logger.warning("Failed to queue ATTEMPT_START event: %s", ex)

    def on_attempt_end(record: AttemptRecord):
        event = {
            "type": "ATTEMPT_END",
            "payload": asdict(record),
        }
        try:
            fut = asyncio.run_coroutine_threadsafe(state.event_queue.put(event), loop)
            fut.result(timeout=5.0)
        except Exception as ex:
            logger.warning("Failed to queue ATTEMPT_END event: %s", ex)

    try:
        # Run blocking controller in thread pool
        outcome: RecoveryOutcome = await asyncio.to_thread(
            controller.run,
            target_file=target_path,
            test_command=state.request.test_command,
            test_file=test_path,
            on_attempt_start=on_attempt_start,
            on_attempt_end=on_attempt_end,
        )
        state.outcome = outcome

        # Push completion event
        completion_event = {
            "type": "RECOVERY_COMPLETE",
            "payload": {
                "status": outcome.status,
                "total_attempts": outcome.total_attempts,
                "total_cost_usd": outcome.total_cost_usd,
                "total_duration_ms": outcome.total_duration_ms,
                "unified_diff": outcome.unified_diff,
                "message": outcome.message,
                "target_file": str(target_path),
                "selected_provider": outcome.selected_provider,
            },
        }
        await state.event_queue.put(completion_event)
    except Exception as ex:
        logger.exception("Error in recovery worker:")
        err_event = {
            "type": "RECOVERY_COMPLETE",
            "payload": {
                "status": "ERROR",
                "total_attempts": 0,
                "total_cost_usd": 0.0,
                "total_duration_ms": 0.0,
                "unified_diff": "",
                "message": f"Execution error: {str(ex)}",
                "target_file": str(target_path),
            },
        }
        await state.event_queue.put(err_event)
    finally:
        state.is_active = False
        # Sentinel to close stream
        await state.event_queue.put(None)


@router.get("/stream/{task_id}")
async def stream_recovery_events(
    task_id: str,
    token: str = Depends(verify_sse_token),
) -> StreamingResponse:
    """Stream real-time SSE recovery events to frontend."""
    state = task_manager.get_task(task_id)
    if not state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recovery task '{task_id}' not found.",
        )

    async def event_generator() -> AsyncGenerator[str, None]:
        while True:
            event = await state.event_queue.get()
            if event is None:
                # Stream finished
                break
            payload_str = json.dumps(event)
            yield f"data: {payload_str}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def calculate_file_hash(path: Path) -> str:
    """Calculate SHA-256 hash of a file's contents."""
    import hashlib
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ApplyPatchRequest(BaseModel):
    task_id: str = Field(..., description="Task identifier from recovery run.")
    target_file: str = Field(..., description="Target file path.")
    expected_hash: Optional[str] = Field(None, description="Optional SHA-256 baseline hash to prevent overwriting concurrent edits.")


@router.post("/apply")
async def apply_verified_patch(
    req: ApplyPatchRequest,
    request: Request,
    token: str = Depends(require_session_token),
) -> dict:
    """Safely apply verified patch in-place with SHA-256 concurrency gating."""
    workspace_root = getattr(request.app.state, "workspace", Path(".").resolve())
    target_path = validate_path_within_workspace(req.target_file, workspace_root)

    state = task_manager.get_task(req.task_id)
    if not state or not state.outcome:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No completed recovery outcome found for task '{req.task_id}'.",
        )

    if state.outcome.status != "SUCCESS" or not state.outcome.patched_content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot apply unverified patch. Task status: {state.outcome.status}.",
        )

    # A completed API task may not yet have a persisted artifact (older
    # in-memory outcomes, or a worker that failed before persistence).
    # Materialize it first, then use the same authoritative applier as the CLI
    # and TUI. There is no direct-write exception.
    run_id = state.outcome.run_id
    if not run_id:
        # Fail closed: a current-file hash is not evidence of the file's state
        # when the candidate was generated. Require a recorded baseline or an
        # explicit caller-supplied expected hash.
        recorded_baseline = (state.baseline_hash or state.outcome.baseline_hash or "").strip()
        requested_baseline = (req.expected_hash or "").strip()
        if recorded_baseline and requested_baseline and recorded_baseline != requested_baseline:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "BASELINE_MISMATCH: recorded recovery baseline and request expected_hash "
                    "do not match. No file was modified."
                ),
            )
        baseline_hash = recorded_baseline or requested_baseline
        if not baseline_hash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "BASELINE_UNAVAILABLE: refusing to apply an artifact-less outcome without "
                    "a trustworthy original baseline. Supply expected_hash or replay the recovery. "
                    "No file was modified."
                ),
            )

        current_hash = calculate_file_hash(target_path)
        if current_hash != baseline_hash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Target file '{target_path.name}' was modified on disk concurrently "
                    "since recovery started. SHA-256 mismatch."
                ),
            )

        if state.outcome.target_file:
            recorded_target = Path(state.outcome.target_file)
            if not recorded_target.is_absolute():
                recorded_target = workspace_root / recorded_target
            if recorded_target.resolve() != target_path:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Target file mismatch: outcome targets '{state.outcome.target_file}', "
                        f"request targets '{req.target_file}'."
                    ),
                )

        target_rel = target_path.relative_to(workspace_root).as_posix()
        run_id = f"api-{state.task_id}-{uuid.uuid4().hex[:8]}"
        try:
            CandidateArtifactStore.save_run_artifact(
                run_id=run_id,
                target_file=target_rel,
                test_command=state.request.test_command,
                baseline_hash=baseline_hash,
                status=state.outcome.status,
                verified=True,
                tier_used=state.outcome.tier_used,
                attempts=state.outcome.total_attempts,
                cost_usd=state.outcome.total_cost_usd,
                duration_ms=state.outcome.total_duration_ms,
                patched_content=state.outcome.patched_content,
                unified_diff=state.outcome.unified_diff,
                workspace_root=workspace_root,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to materialize candidate artifact: {exc}",
            ) from exc
        state.outcome.run_id = run_id

    try:
        CandidateArtifactStore.set_approval(
            run_id,
            approved=True,
            workspace_root=workspace_root,
            approved_by="interactive:api",
            approval_source="api-session-token",
        )
        result = AuthoritativePatchApplier.apply(
            run_id=run_id,
            workspace_root=workspace_root,
            expected_target=req.target_file,
            require_approval=True,
        )
    except CandidateApplicationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message)

    if not result.success:
        is_conflict = result.error_code in (
            "STALE_TARGET_CONFLICT",
            "CONCURRENT_MODIFICATION_DETECTED",
        )
        detail = result.message
        if is_conflict:
            detail = (
                f"Target file '{target_path.name}' was modified on disk concurrently "
                f"since recovery started. SHA-256 mismatch ({result.error_code})."
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT if is_conflict else status.HTTP_400_BAD_REQUEST,
            detail=detail,
        )

    # Cache diff to .argus/last_run.diff
    dot_argus = workspace_root / ".argus"
    dot_argus.mkdir(parents=True, exist_ok=True)
    if state.outcome.unified_diff:
        (dot_argus / "last_run.diff").write_text(state.outcome.unified_diff, encoding="utf-8")

    return {
        "status": "applied",
        "target_file": str(target_path),
        "message": f"Successfully applied verified patch to '{target_path.name}'.",
        "new_hash": calculate_file_hash(target_path),
    }


@router.get("/diff/{task_id}")
async def get_task_diff(
    task_id: str,
    token: str = Depends(require_session_token),
) -> dict:
    """Retrieve verified diff and metadata for a completed task."""
    state = task_manager.get_task(task_id)
    if not state or not state.outcome:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found or outcome unavailable.",
        )
    return {
        "task_id": task_id,
        "status": state.outcome.status,
        "unified_diff": state.outcome.unified_diff,
        "target_file": state.outcome.target_file,
    }

