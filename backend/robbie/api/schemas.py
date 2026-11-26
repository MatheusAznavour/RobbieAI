"""Pydantic models for Robbie backend HTTP and WebSocket payloads."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Response payload returned by GET /health."""

    status: Literal["ok"] = "ok"
    service: Literal["robbie-backend"] = "robbie-backend"
    version: str
    host: str
    port: int
    pid: int
    uptime_seconds: float = Field(ge=0.0)
    websocket_connections: int = Field(ge=0)
    timestamp: str


class StatusEvent(BaseModel):
    """Backend status payload emitted over the authenticated WebSocket."""

    type: Literal["status"] = "status"
    status: str
    version: str
    host: str
    port: int
    pid: int
    uptime_seconds: float = Field(ge=0.0)
    timestamp: str


class PongEvent(BaseModel):
    """Response payload for WebSocket ping messages."""

    type: Literal["pong"] = "pong"
    request_id: str | None = None
    timestamp: str


class ErrorEvent(BaseModel):
    """Structured WebSocket error response for malformed client messages."""

    type: Literal["error"] = "error"
    code: str
    detail: str
    timestamp: str
