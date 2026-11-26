"""Typed configuration for the Robbie local FastAPI backend."""

from __future__ import annotations

import os
import secrets
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

LOOPBACK_HOST: Final[str] = "127.0.0.1"
DEFAULT_ALLOWED_ORIGINS: Final[tuple[str, ...]] = ("robbie://desktop",)
MIN_TOKEN_LENGTH: Final[int] = 16


def generate_session_token() -> str:
    """Generate a cryptographically secure random bearer token."""
    return secrets.token_urlsafe(32)


class BackendSettings(BaseModel):
    """Validated runtime settings for the Robbie local backend server."""

    model_config = ConfigDict(frozen=True)

    host: str = Field(default=LOOPBACK_HOST)
    port: int = Field(default=0, ge=0, le=65535)
    api_token: SecretStr = Field(default_factory=lambda: SecretStr(generate_session_token()))
    allowed_origins: tuple[str, ...] = Field(default=DEFAULT_ALLOWED_ORIGINS)
    log_level: str = Field(default="INFO")

    @field_validator("host")
    @classmethod
    def validate_loopback_host(cls, value: str) -> str:
        normalized = value.strip()
        if normalized != LOOPBACK_HOST:
            raise ValueError(
                f"Backend host must be '{LOOPBACK_HOST}' for local security; got '{value}'."
            )
        return normalized

    @field_validator("api_token")
    @classmethod
    def validate_api_token(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value().strip()
        if len(raw) < MIN_TOKEN_LENGTH:
            raise ValueError(
                f"API bearer token must be at least {MIN_TOKEN_LENGTH} characters long."
            )
        return SecretStr(raw)

    @field_validator("allowed_origins")
    @classmethod
    def validate_allowed_origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(origin.strip() for origin in value if origin.strip())
        if not cleaned:
            raise ValueError("At least one allowed origin must be configured.")
        if "*" in cleaned:
            raise ValueError("Wildcard '*' origin is not permitted.")
        return cleaned

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        valid_levels = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if normalized not in valid_levels:
            raise ValueError(f"Unsupported log level '{value}'. Expected one of {valid_levels}.")
        return normalized

    @classmethod
    def from_env(cls) -> BackendSettings:
        """Load backend settings from process environment variables."""
        host = os.environ.get("ROBBIE_BACKEND_HOST", LOOPBACK_HOST)
        port_raw = os.environ.get("ROBBIE_BACKEND_PORT", "0").strip()
        try:
            port = int(port_raw) if port_raw else 0
        except ValueError as exc:
            raise ValueError(f"ROBBIE_BACKEND_PORT must be an integer; got '{port_raw}'.") from exc

        token_raw = os.environ.get("ROBBIE_API_TOKEN")
        if token_raw is not None:
            api_token = SecretStr(token_raw)
        else:
            api_token = SecretStr(generate_session_token())

        origins_raw = os.environ.get("ROBBIE_ALLOWED_ORIGINS")
        if origins_raw is not None:
            allowed_origins = tuple(item.strip() for item in origins_raw.split(",") if item.strip())
        else:
            allowed_origins = DEFAULT_ALLOWED_ORIGINS

        log_level = os.environ.get("ROBBIE_LOG_LEVEL", "INFO")

        return cls(
            host=host,
            port=port,
            api_token=api_token,
            allowed_origins=allowed_origins,
            log_level=log_level,
        )
