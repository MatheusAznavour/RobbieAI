"""Authentication, origin validation, and token-redacting logging utilities."""

from __future__ import annotations

import logging
import secrets
from typing import Final

from fastapi import HTTPException, Request, WebSocket, status

BEARER_PREFIX: Final[str] = "Bearer "
REDACTED_MARKER: Final[str] = "[REDACTED]"


class TokenRedactingFilter(logging.Filter):
    """Logging filter that scrubs the active API bearer token from log output."""

    def __init__(self, secret_token: str) -> None:
        super().__init__()
        self._secret_token = secret_token

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secret_token:
            return True

        if isinstance(record.msg, str) and self._secret_token in record.msg:
            record.msg = record.msg.replace(self._secret_token, REDACTED_MARKER)

        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    arg.replace(self._secret_token, REDACTED_MARKER)
                    if isinstance(arg, str) and self._secret_token in arg
                    else arg
                    for arg in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: (
                        v.replace(self._secret_token, REDACTED_MARKER)
                        if isinstance(v, str) and self._secret_token in v
                        else v
                    )
                    for k, v in record.args.items()
                }
        return True


def configure_logging(log_level: str, secret_token: str) -> None:
    """Configure structured stderr logging with bearer-token redaction."""
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level, logging.INFO))

    redactor = TokenRedactingFilter(secret_token)
    for existing_filter in list(root_logger.filters):
        if isinstance(existing_filter, TokenRedactingFilter):
            root_logger.removeFilter(existing_filter)
    root_logger.addFilter(redactor)

    for logger_name in ("robbie", "uvicorn", "uvicorn.error", "uvicorn.access"):
        target = logging.getLogger(logger_name)
        for existing_filter in list(target.filters):
            if isinstance(existing_filter, TokenRedactingFilter):
                target.removeFilter(existing_filter)
        target.addFilter(redactor)


def extract_bearer_token(authorization_header: str | None) -> str | None:
    """Extract the bearer token from an HTTP Authorization header."""
    if not authorization_header:
        return None
    if not authorization_header.startswith(BEARER_PREFIX):
        return None
    token = authorization_header[len(BEARER_PREFIX) :].strip()
    return token or None


def verify_bearer_token(provided: str | None, expected: str) -> bool:
    """Verify a bearer token using constant-time comparison."""
    if not provided or not expected:
        return False
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def is_origin_allowed(origin: str | None, allowed_origins: tuple[str, ...]) -> bool:
    """Check whether a request Origin header matches the configured allowlist."""
    if not origin:
        return False
    return origin.strip() in allowed_origins


def require_http_auth(request: Request) -> None:
    """FastAPI dependency enforcing Bearer token authentication on HTTP routes."""
    expected_token: str = request.app.state.settings.api_token.get_secret_value()
    provided_token = extract_bearer_token(request.headers.get("authorization"))
    if not verify_bearer_token(provided_token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing authentication credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def validate_websocket_handshake(websocket: WebSocket) -> tuple[bool, str]:
    """Validate WebSocket Origin and Bearer token prior to accepting the connection."""
    settings = websocket.app.state.settings
    origin = websocket.headers.get("origin")
    if not is_origin_allowed(origin, settings.allowed_origins):
        return False, "Origin not allowed."

    provided_token = extract_bearer_token(websocket.headers.get("authorization"))
    expected_token = settings.api_token.get_secret_value()
    if not verify_bearer_token(provided_token, expected_token):
        return False, "Invalid or missing authentication credentials."

    return True, ""
