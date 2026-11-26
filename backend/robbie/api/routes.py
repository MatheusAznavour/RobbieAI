"""Authenticated HTTP and WebSocket routes for the Robbie local backend."""

from __future__ import annotations

import json
import logging

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)

import robbie
from robbie.agents.conversation import (
    ConversationOrchestrator,
    ProviderRequestError,
    ProviderTimeoutError,
)
from robbie.agents.runner import (
    ApprovalConflictError,
    ApprovalMismatchError,
    EmergencyStopActiveError,
    TaskRunner,
)
from robbie.api.schemas import (
    ApprovalDecisionRequest,
    ChatMessageResponse,
    ConversationDetailResponse,
    ConversationSummaryResponse,
    CreateConversationRequest,
    CreateTaskRequest,
    EmergencyStopResetResponse,
    EmergencyStopResponse,
    ErrorEvent,
    HealthResponse,
    PongEvent,
    RenameConversationRequest,
    SendMessageRequest,
    SendMessageResponse,
    SettingsSnapshotResponse,
    StatusEvent,
    TaskStatusResponse,
    UpdateSettingsRequest,
)
from robbie.core.runtime import BackendRuntime, utc_now_iso
from robbie.core.security import (
    get_secret_scrubber,
    require_http_auth,
    validate_websocket_handshake,
)
from robbie.memory.conversations import ConversationStore
from robbie.providers.settings import ProviderSettingsManager

logger = logging.getLogger("robbie.api")

router = APIRouter()


def _build_status_event(runtime: BackendRuntime) -> dict[str, object]:
    is_estopped = bool(runtime.task_runner and runtime.task_runner.is_emergency_stopped)
    return StatusEvent(
        status=runtime.status,
        version=robbie.__version__,
        host=runtime.host,
        port=runtime.port,
        pid=runtime.pid,
        uptime_seconds=runtime.uptime_seconds,
        emergency_stopped=is_estopped,
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


def _require_conversation_store(request: Request) -> ConversationStore:
    runtime: BackendRuntime = request.app.state.runtime
    if runtime.conversation_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Conversation store is not initialized.",
        )
    return runtime.conversation_store


def _require_orchestrator(request: Request) -> ConversationOrchestrator:
    runtime: BackendRuntime = request.app.state.runtime
    if runtime.orchestrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Conversation orchestrator is not initialized.",
        )
    return runtime.orchestrator


def _require_settings_manager(request: Request) -> ProviderSettingsManager:
    runtime: BackendRuntime = request.app.state.runtime
    if runtime.settings_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Settings manager is not initialized.",
        )
    return runtime.settings_manager


@router.get(
    "/health",
    response_model=HealthResponse,
    dependencies=[Depends(require_http_auth)],
)
async def get_health(request: Request) -> HealthResponse:
    """Return authenticated health and runtime metadata."""
    runtime: BackendRuntime = request.app.state.runtime
    is_estopped = bool(runtime.task_runner and runtime.task_runner.is_emergency_stopped)
    return HealthResponse(
        version=robbie.__version__,
        host=runtime.host,
        port=runtime.port,
        pid=runtime.pid,
        uptime_seconds=runtime.uptime_seconds,
        websocket_connections=runtime.connections.active_count,
        emergency_stopped=is_estopped,
        timestamp=utc_now_iso(),
    )


# ---------------------------------------------------------------------------
# Conversations & Conversational Chat
# ---------------------------------------------------------------------------


@router.get(
    "/conversations",
    response_model=list[ConversationSummaryResponse],
    dependencies=[Depends(require_http_auth)],
)
async def list_conversations(
    request: Request,
    q: str = Query(default="", max_length=200),
) -> list[ConversationSummaryResponse]:
    store = _require_conversation_store(request)
    return [ConversationSummaryResponse(**item) for item in store.list_conversations(query=q)]


@router.post(
    "/conversations",
    response_model=ConversationDetailResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_http_auth)],
)
async def create_conversation(
    request: Request,
    payload: CreateConversationRequest | None = None,
) -> ConversationDetailResponse:
    store = _require_conversation_store(request)
    record = store.create_conversation(title=payload.title if payload else None)
    return ConversationDetailResponse(**record.to_detail_dict(), tasks=[])


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
    dependencies=[Depends(require_http_auth)],
)
async def get_conversation(
    conversation_id: str,
    request: Request,
) -> ConversationDetailResponse:
    store = _require_conversation_store(request)
    runner = _require_task_runner(request)
    record = store.get_conversation(conversation_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )

    tasks: list[TaskStatusResponse] = []
    for task_id in record.task_ids:
        task_rec = runner.get_task(task_id)
        if task_rec is not None:
            tasks.append(TaskStatusResponse(**task_rec.to_public_dict()))

    return ConversationDetailResponse(**record.to_detail_dict(), tasks=tasks)


@router.patch(
    "/conversations/{conversation_id}",
    response_model=ConversationSummaryResponse,
    dependencies=[Depends(require_http_auth)],
)
async def rename_conversation(
    conversation_id: str,
    payload: RenameConversationRequest,
    request: Request,
) -> ConversationSummaryResponse:
    store = _require_conversation_store(request)
    try:
        record = store.rename_conversation(conversation_id, payload.title)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ConversationSummaryResponse(**record.to_summary_dict())


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_http_auth)],
)
async def delete_conversation(
    conversation_id: str,
    request: Request,
) -> None:
    store = _require_conversation_store(request)
    try:
        store.delete_conversation(conversation_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=SendMessageResponse,
    dependencies=[Depends(require_http_auth)],
)
async def send_conversation_message(
    conversation_id: str,
    payload: SendMessageRequest,
    request: Request,
) -> SendMessageResponse:
    store = _require_conversation_store(request)
    orchestrator = _require_orchestrator(request)
    scrubber = get_secret_scrubber()

    try:
        outcome = await orchestrator.send_message(
            conversation_id,
            payload.content,
            mode=payload.mode,
        )
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except EmergencyStopActiveError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (ProviderTimeoutError, TimeoutError) as exc:
        safe_msg = scrubber.scrub_text(
            str(exc) or "Model provider request timed out before returning a response."
        )
        logger.error("Conversation model request timed out: %s", safe_msg)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=safe_msg,
        ) from exc
    except ProviderRequestError as exc:
        safe_msg = scrubber.scrub_text(str(exc) or "Model provider request failed.")
        logger.error("Conversation model request failed: %s", safe_msg)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=safe_msg,
        ) from exc
    except RuntimeError as exc:
        safe_msg = scrubber.scrub_text(str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=safe_msg,
        ) from exc
    except Exception as exc:
        safe_msg = scrubber.scrub_text(str(exc) or type(exc).__name__)
        logger.error("Failed to process conversation message: %s", safe_msg)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Model invocation failed: {safe_msg}",
        ) from exc

    conv = store.get_conversation(conversation_id)
    assert conv is not None
    return SendMessageResponse(
        conversation=ConversationSummaryResponse(**conv.to_summary_dict()),
        user_message=ChatMessageResponse(**outcome.user_message.to_dict()),
        assistant_message=ChatMessageResponse(**outcome.assistant_message.to_dict()),
        task=TaskStatusResponse(**outcome.task.to_public_dict()) if outcome.task else None,
    )


@router.post(
    "/conversations/{conversation_id}/messages/{message_id}/retry",
    response_model=SendMessageResponse,
    dependencies=[Depends(require_http_auth)],
)
async def retry_conversation_message(
    conversation_id: str,
    message_id: str,
    request: Request,
) -> SendMessageResponse:
    store = _require_conversation_store(request)
    conv = store.get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )

    target = next((m for m in conv.messages if m.message_id == message_id), None)
    if target is None or target.role != "user":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Retryable user message not found.",
        )

    return await send_conversation_message(
        conversation_id,
        SendMessageRequest(content=target.content, mode="auto"),
        request,
    )


# ---------------------------------------------------------------------------
# Provider & Model Settings (Zero Secrets)
# ---------------------------------------------------------------------------


@router.get(
    "/settings",
    response_model=SettingsSnapshotResponse,
    dependencies=[Depends(require_http_auth)],
)
async def get_settings(request: Request) -> SettingsSnapshotResponse:
    manager = _require_settings_manager(request)
    return SettingsSnapshotResponse(**manager.get_public_snapshot())


@router.put(
    "/settings",
    response_model=SettingsSnapshotResponse,
    dependencies=[Depends(require_http_auth)],
)
async def update_settings(
    payload: UpdateSettingsRequest,
    request: Request,
) -> SettingsSnapshotResponse:
    manager = _require_settings_manager(request)
    manager.update_selection(payload.selection)
    return SettingsSnapshotResponse(**manager.get_public_snapshot())


# ---------------------------------------------------------------------------
# Desktop Tasks, Approvals, and Emergency Stop
# ---------------------------------------------------------------------------


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
    store = request.app.state.runtime.conversation_store
    try:
        record = await runner.start_task(
            payload.instruction,
            payload.config,
            conversation_id=payload.conversation_id,
        )
    except EmergencyStopActiveError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if store is not None and payload.conversation_id:
        store.link_task(payload.conversation_id, record.task_id)
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
