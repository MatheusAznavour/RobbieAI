"""Authenticated HTTP and WebSocket routes for the Robbie local backend."""

from __future__ import annotations

import json
import logging

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)

import robbie
from robbie.agents.runner import (
    ApprovalConflictError,
    ApprovalMismatchError,
    EmergencyStopActiveError,
    TaskRunner,
)
from robbie.api.schemas import (
    ApprovalDecisionRequest,
    CreateTaskRequest,
    EmergencyStopResetResponse,
    EmergencyStopResponse,
    ErrorEvent,
    HealthResponse,
    PongEvent,
    StatusEvent,
    TaskStatusResponse,
)
from robbie.core.runtime import BackendRuntime, utc_now_iso
from robbie.core.security import require_http_auth, validate_websocket_handshake

logger = logging.getLogger("robbie.api")

router = APIRouter()


def _build_status_event(runtime: BackendRuntime) -> dict[str, object]:
    return StatusEvent(
        status=runtime.status,
        version=robbie.__version__,
        host=runtime.host,
        port=runtime.port,
        pid=runtime.pid,
        uptime_seconds=runtime.uptime_seconds,
        timestamp=utc_now_iso(),
    ).model_dump()


def _require_task_runner(request: Request) -> TaskRunner:
    runtime: BackendRuntime = request.app.state.runtime
    if runtime.task_runner is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task runner is not initialized.",
        )
    return runtime.task_runner


@router.get(
    "/health",
    response_model=HealthResponse,
    dependencies=[Depends(require_http_auth)],
)
async def get_health(request: Request) -> HealthResponse:
    """Return authenticated health and runtime metadata."""
    runtime: BackendRuntime = request.app.state.runtime
    return HealthResponse(
        version=robbie.__version__,
        host=runtime.host,
        port=runtime.port,
        pid=runtime.pid,
        uptime_seconds=runtime.uptime_seconds,
        websocket_connections=runtime.connections.active_count,
        timestamp=utc_now_iso(),
    )


@router.post(
    "/tasks",
    response_model=TaskStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_http_auth)],
)
async def create_task(
    payload: CreateTaskRequest,
    request: Request,
) -> TaskStatusResponse:
    """Start a new non-blocking Agent-S S3 task (rejected if emergency stop is active)."""
    runner = _require_task_runner(request)
    try:
        record = await runner.start_task(payload.instruction, payload.config)
    except EmergencyStopActiveError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return TaskStatusResponse(**record.to_public_dict())


@router.get(
    "/tasks",
    response_model=list[TaskStatusResponse],
    dependencies=[Depends(require_http_auth)],
)
async def list_tasks(request: Request) -> list[TaskStatusResponse]:
    """List current and recent in-memory tasks."""
    runner = _require_task_runner(request)
    return [TaskStatusResponse(**item) for item in runner.list_tasks()]


@router.get(
    "/tasks/{task_id}",
    response_model=TaskStatusResponse,
    dependencies=[Depends(require_http_auth)],
)
async def get_task(task_id: str, request: Request) -> TaskStatusResponse:
    """Inspect current state and pending approval for a task."""
    runner = _require_task_runner(request)
    record = runner.get_task(task_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    return TaskStatusResponse(**record.to_public_dict())


@router.post(
    "/tasks/{task_id}/pause",
    response_model=TaskStatusResponse,
    dependencies=[Depends(require_http_auth)],
)
async def pause_task(task_id: str, request: Request) -> TaskStatusResponse:
    """Pause an active task before its next step or action."""
    runner = _require_task_runner(request)
    try:
        record = await runner.pause_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return TaskStatusResponse(**record.to_public_dict())


@router.post(
    "/tasks/{task_id}/resume",
    response_model=TaskStatusResponse,
    dependencies=[Depends(require_http_auth)],
)
async def resume_task(task_id: str, request: Request) -> TaskStatusResponse:
    """Resume a paused task (rejected if interrupted or emergency-stopped)."""
    runner = _require_task_runner(request)
    try:
        record = await runner.resume_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return TaskStatusResponse(**record.to_public_dict())


@router.post(
    "/tasks/{task_id}/cancel",
    response_model=TaskStatusResponse,
    dependencies=[Depends(require_http_auth)],
)
async def cancel_task(task_id: str, request: Request) -> TaskStatusResponse:
    """Cooperatively cancel an active or waiting task and invalidate pending approvals."""
    runner = _require_task_runner(request)
    try:
        record = await runner.cancel_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return TaskStatusResponse(**record.to_public_dict())


@router.post(
    "/tasks/{task_id}/approvals/{approval_id}",
    response_model=TaskStatusResponse,
    dependencies=[Depends(require_http_auth)],
)
async def resolve_approval(
    task_id: str,
    approval_id: str,
    payload: ApprovalDecisionRequest,
    request: Request,
) -> TaskStatusResponse:
    """Approve or deny a pending sensitive action bound to task_id, step_id, and action_digest."""
    runner = _require_task_runner(request)
    try:
        record = await runner.resolve_approval(
            task_id,
            approval_id,
            approved=payload.approved,
            step_id=payload.step_id,
            action_digest=payload.action_digest,
            reason=payload.reason,
        )
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (ApprovalConflictError, ApprovalMismatchError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return TaskStatusResponse(**record.to_public_dict())


@router.post(
    "/emergency-stop",
    response_model=EmergencyStopResponse,
    dependencies=[Depends(require_http_auth)],
)
async def emergency_stop(request: Request) -> EmergencyStopResponse:
    """Engage persistent emergency stop and halt all active tasks immediately."""
    runner = _require_task_runner(request)
    affected = await runner.emergency_stop()
    return EmergencyStopResponse(affected_task_ids=affected, timestamp=utc_now_iso())


@router.post(
    "/emergency-stop/reset",
    response_model=EmergencyStopResetResponse,
    dependencies=[Depends(require_http_auth)],
)
async def reset_emergency_stop(request: Request) -> EmergencyStopResetResponse:
    """Explicitly reset the persistent emergency-stop latch to allow new tasks."""
    runner = _require_task_runner(request)
    await runner.reset_emergency_stop()
    return EmergencyStopResetResponse(timestamp=utc_now_iso())


@router.websocket("/ws/status")
async def status_websocket(websocket: WebSocket) -> None:
    """Authenticated WebSocket channel for backend status, task events, and controls."""
    is_valid, reason = validate_websocket_handshake(websocket)
    if not is_valid:
        logger.warning("Rejected WebSocket handshake: %s", reason)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=reason)
        return

    runtime: BackendRuntime = websocket.app.state.runtime
    await websocket.accept()
    await runtime.connections.register(websocket)
    logger.info("Authenticated WebSocket client connected.")

    try:
        await websocket.send_json(_build_status_event(runtime))

        while True:
            raw_text = await websocket.receive_text()
            try:
                message = json.loads(raw_text)
            except json.JSONDecodeError:
                error = ErrorEvent(
                    code="invalid_json",
                    detail="Message must be valid JSON.",
                    timestamp=utc_now_iso(),
                )
                await websocket.send_json(error.model_dump())
                continue

            if not isinstance(message, dict):
                error = ErrorEvent(
                    code="invalid_payload",
                    detail="Message must be a JSON object.",
                    timestamp=utc_now_iso(),
                )
                await websocket.send_json(error.model_dump())
                continue

            msg_type = message.get("type")
            if msg_type == "ping":
                request_id = message.get("request_id")
                pong = PongEvent(
                    request_id=request_id if isinstance(request_id, str) else None,
                    timestamp=utc_now_iso(),
                )
                await websocket.send_json(pong.model_dump())
            elif msg_type == "status_request":
                await websocket.send_json(_build_status_event(runtime))
            elif msg_type == "emergency_stop":
                if runtime.task_runner is not None:
                    await runtime.task_runner.emergency_stop()
            else:
                error = ErrorEvent(
                    code="unsupported_message_type",
                    detail=(
                        "Supported message types are 'ping', 'status_request', "
                        "and 'emergency_stop'."
                    ),
                    timestamp=utc_now_iso(),
                )
                await websocket.send_json(error.model_dump())
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected.")
    finally:
        await runtime.connections.unregister(websocket)
