"""Authenticated HTTP and WebSocket routes for the Robbie local backend."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect, status

import robbie
from robbie.api.schemas import ErrorEvent, HealthResponse, PongEvent, StatusEvent
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


@router.websocket("/ws/status")
async def status_websocket(websocket: WebSocket) -> None:
    """Authenticated WebSocket channel for backend status and ping/pong checks."""
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
            else:
                error = ErrorEvent(
                    code="unsupported_message_type",
                    detail="Supported message types are 'ping' and 'status_request'.",
                    timestamp=utc_now_iso(),
                )
                await websocket.send_json(error.model_dump())
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected.")
    finally:
        await runtime.connections.unregister(websocket)
