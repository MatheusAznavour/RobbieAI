"""Pydantic models for Robbie backend HTTP and WebSocket payloads."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from robbie.providers.config import AgentRuntimeConfig


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


class CreateTaskRequest(BaseModel):
    """Authenticated request payload to launch an Agent-S S3 task."""

    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=4000)
    config: AgentRuntimeConfig

    @field_validator("instruction")
    @classmethod
    def validate_instruction(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Task instruction must not be empty.")
        return cleaned


class ApprovalDecisionRequest(BaseModel):
    """User approval or denial payload for a pending sensitive action."""

    model_config = ConfigDict(extra="forbid")

    approved: bool
    reason: str | None = Field(default=None, max_length=500)


class PendingApprovalResponse(BaseModel):
    approval_id: str
    task_id: str
    step_id: str
    action: dict[str, Any]
    risk_level: str
    category: str
    reasons: list[str]
    created_at: str


class TaskStatusResponse(BaseModel):
    task_id: str
    instruction: str
    state: str
    current_step: int
    max_steps: int
    last_step_id: str | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str
    pending_approval: PendingApprovalResponse | None = None


class EmergencyStopResponse(BaseModel):
    status: Literal["emergency_stopped"] = "emergency_stopped"
    affected_task_ids: list[str]
    timestamp: str
