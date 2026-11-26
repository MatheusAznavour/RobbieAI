"""Pydantic models for Robbie backend HTTP and WebSocket payloads."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from robbie.providers.config import AgentRuntimeConfig
from robbie.providers.settings import ModelSelectionState


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
    emergency_stopped: bool = False
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
    emergency_stopped: bool = False
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
    conversation_id: str | None = Field(default=None, max_length=128)

    @field_validator("instruction")
    @classmethod
    def validate_instruction(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Task instruction must not be empty.")
        return cleaned


class ApprovalDecisionRequest(BaseModel):
    """User approval or denial bound strictly to step_id and action_digest."""

    model_config = ConfigDict(extra="forbid")

    approved: bool
    step_id: str = Field(min_length=1, max_length=256)
    action_digest: str = Field(min_length=64, max_length=64)
    reason: str | None = Field(default=None, max_length=500)


class PendingApprovalResponse(BaseModel):
    approval_id: str
    task_id: str
    step_id: str
    action_digest: str
    action: dict[str, Any]
    risk_level: str
    category: str
    reasons: list[str]
    created_at: str


class TaskStatusResponse(BaseModel):
    task_id: str
    conversation_id: str | None = None
    instruction: str
    state: str
    current_step: int
    max_steps: int
    last_step_id: str | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str
    pending_approval: PendingApprovalResponse | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)


class EmergencyStopResponse(BaseModel):
    status: Literal["emergency_stopped"] = "emergency_stopped"
    affected_task_ids: list[str]
    timestamp: str


class EmergencyStopResetResponse(BaseModel):
    status: Literal["ready"] = "ready"
    emergency_stopped: Literal[False] = False
    timestamp: str


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=64)


class RenameConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=64)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Conversation title must not be empty.")
        return cleaned


class SendMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=8000)
    mode: Literal["auto", "chat", "desktop"] = "auto"

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Message content must not be empty.")
        return cleaned


class ChatMessageResponse(BaseModel):
    message_id: str
    conversation_id: str
    role: Literal["user", "assistant"]
    content: str
    kind: Literal["chat", "task_started", "task_summary", "error"]
    task_id: str | None = None
    created_at: str


class ConversationSummaryResponse(BaseModel):
    conversation_id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int
    task_count: int
    last_message_preview: str


class ConversationDetailResponse(BaseModel):
    conversation_id: str
    title: str
    created_at: str
    updated_at: str
    messages: list[ChatMessageResponse]
    task_ids: list[str]
    tasks: list[TaskStatusResponse] = Field(default_factory=list)


class SendMessageResponse(BaseModel):
    conversation: ConversationSummaryResponse
    user_message: ChatMessageResponse
    assistant_message: ChatMessageResponse
    task: TaskStatusResponse | None = None


class SettingsSnapshotResponse(BaseModel):
    selection: ModelSelectionState
    providers: list[dict[str, Any]]
    chat_ready: bool
    chat_configuration_errors: list[str]
    desktop_ready: bool
    desktop_configuration_errors: list[str]
    safety_policy_locked: bool
    requires_restart_on_env_change: bool


class UpdateSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection: ModelSelectionState
