"""Runtime state tracking and WebSocket connection lifecycle management."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime

from fastapi import WebSocket


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(UTC).isoformat()


class ConnectionManager:
    """Tracks active authenticated WebSocket connections for clean shutdown and broadcast."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def register(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.add(websocket)

    async def unregister(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    @property
    def active_count(self) -> int:
        return len(self._connections)

    async def close_all(self) -> None:
        """Notify and close all active WebSocket connections gracefully."""
        async with self._lock:
            targets = list(self._connections)
            self._connections.clear()

        payload = {"type": "shutdown", "timestamp": utc_now_iso()}
        for websocket in targets:
            try:
                await websocket.send_json(payload)
            except Exception:
                pass
            try:
                await websocket.close(code=1001, reason="Server shutting down")
            except Exception:
                pass


class BackendRuntime:
    """Holds live runtime metadata for the FastAPI application instance."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.pid = os.getpid()
        self.status = "starting"
        self.started_at = time.monotonic()
        self.connections = ConnectionManager()

    def set_bound_port(self, port: int) -> None:
        self.port = port

    def mark_ready(self) -> None:
        self.status = "ready"

    def mark_shutting_down(self) -> None:
        self.status = "shutting_down"

    @property
    def uptime_seconds(self) -> float:
        return round(max(0.0, time.monotonic() - self.started_at), 3)
