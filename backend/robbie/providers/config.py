"""Provider-agnostic configuration models for Agent-S S3 worker and grounding engines."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

SupportedProvider = Literal[
    "openai",
    "anthropic",
    "gemini",
    "azure",
    "open_router",
    "vllm",
    "ollama",
    "deepseek",
    "qwen",
    "parasail",
    "huggingface",
]


class WorkerModelConfig(BaseModel):
    """Configuration for the primary Vision-Language Model (worker/reflection)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: SupportedProvider
    model: str = Field(min_length=1)
    base_url: str | None = None
    api_key: SecretStr | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    api_version: str | None = None
    organization: str | None = None

    @field_validator("model")
    @classmethod
    def validate_model_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Worker model name must not be empty.")
        return cleaned

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    def to_engine_params(self) -> dict[str, Any]:
        """Build the engine_params dictionary expected by Agent-S S3 LMMAgent."""
        params: dict[str, Any] = {
            "engine_type": self.provider,
            "model": self.model,
        }
        if self.base_url:
            if self.provider == "azure":
                params["azure_endpoint"] = self.base_url
            else:
                params["base_url"] = self.base_url
        if self.api_key is not None:
            secret = self.api_key.get_secret_value().strip()
            if secret:
                params["api_key"] = secret
        if self.temperature is not None:
            params["temperature"] = self.temperature
        if self.api_version:
            params["api_version"] = self.api_version
        if self.organization:
            params["organization"] = self.organization
        return params


class GroundingModelConfig(BaseModel):
    """Configuration for the visual grounding model used for GUI coordinate prediction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: SupportedProvider
    model: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key: SecretStr | None = None
    grounding_width: int = Field(default=1920, gt=0, le=16384)
    grounding_height: int = Field(default=1080, gt=0, le=16384)

    @field_validator("model", "base_url")
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Grounding model and base_url must not be empty.")
        return cleaned

    def to_engine_params(self) -> dict[str, Any]:
        """Build the engine_params_for_grounding dictionary for Agent-S S3."""
        params: dict[str, Any] = {
            "engine_type": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "grounding_width": self.grounding_width,
            "grounding_height": self.grounding_height,
        }
        if self.api_key is not None:
            secret = self.api_key.get_secret_value().strip()
            if secret:
                params["api_key"] = secret
        return params


class AgentRuntimeConfig(BaseModel):
    """Validated runtime configuration for an Agent-S S3 task execution.

    Safety guardrails (LocalEnv disabled, AST action validation, human approval
    requirements) are enforced unconditionally by Robbie and cannot be disabled
    through model or runtime configuration.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    worker: WorkerModelConfig
    grounding: GroundingModelConfig
    max_steps: int = Field(default=15, ge=1, le=100)
    max_trajectory_length: int = Field(default=8, ge=1, le=20)
    enable_reflection: bool = True
    step_timeout_seconds: float = Field(default=120.0, gt=0.0, le=600.0)
    approval_timeout_seconds: float = Field(default=120.0, gt=0.0, le=600.0)
    dry_run: bool = False
