"""Authentication, origin validation, and comprehensive secret redaction utilities."""

from __future__ import annotations

import logging
import re
import secrets
import threading
from typing import Any, Final

from fastapi import HTTPException, Request, WebSocket, status

BEARER_PREFIX: Final[str] = "Bearer "
REDACTED_MARKER: Final[str] = "[REDACTED]"

CREDENTIAL_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # OpenAI / Anthropic / Stripe-style secret keys
    re.compile(r"\bsk-(?:proj-|ant-|live-|test-)?[A-Za-z0-9_\-]{8,}\b"),
    # GitHub classic & fine-grained PATs
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,})\b"),
    # Google API keys
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),
    # Hugging Face access tokens
    re.compile(r"\bhf_[A-Za-z0-9]{10,}\b"),
    # AWS access key IDs
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # Authorization Bearer tokens in logs or error strings
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-+/=]{8,}"),
    # Credit card numbers (13-19 digits grouped by spaces or dashes)
    re.compile(r"(?<!\d)(?:\d{4}[ -]){3}\d{1,7}(?!\d)"),
    re.compile(r"(?<!\d)\d{13,19}(?!\d)"),
)


class SecretScrubber:
    """Thread-safe secret registry and pattern scrubber for logs, errors, and events."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._secrets: set[str] = set()

    def register(self, secret: str | None) -> None:
        """Register a runtime secret so it is scrubbed from all output."""
        if not secret:
            return
        cleaned = secret.strip()
        if len(cleaned) < 6:
            return
        with self._lock:
            self._secrets.add(cleaned)

    def scrub_text(self, text: str) -> str:
        """Remove registered secrets and recognizable credential patterns from text."""
        if not text:
            return text

        scrubbed = text
        with self._lock:
            # Replace longer secrets first to avoid partial overlap leftovers.
            registered = sorted(self._secrets, key=len, reverse=True)

        for secret in registered:
            if secret in scrubbed:
                scrubbed = scrubbed.replace(secret, REDACTED_MARKER)

        for pattern in CREDENTIAL_PATTERNS:
            scrubbed = pattern.sub(REDACTED_MARKER, scrubbed)

        return scrubbed

    def scrub_value(self, value: Any) -> Any:
        """Recursively scrub strings inside dicts, lists, and tuples."""
        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, dict):
            return {k: self.scrub_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.scrub_value(item) for item in value)
        return value


_GLOBAL_SCRUBBER = SecretScrubber()


def get_secret_scrubber() -> SecretScrubber:
    """Return the process-wide secret scrubber shared across logging and task events."""
    return _GLOBAL_SCRUBBER


class TokenRedactingFilter(logging.Filter):
    """Logging filter that scrubs registered secrets and credential patterns from log records."""

    def __init__(self, secret_token: str | None = None, scrubber: SecretScrubber | None = None):
        super().__init__()
        self._scrubber = scrubber or get_secret_scrubber()
        if secret_token:
            self._scrubber.register(secret_token)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._scrubber.scrub_text(record.msg)

        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    self._scrubber.scrub_text(arg) if isinstance(arg, str) else arg
                    for arg in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: (self._scrubber.scrub_text(v) if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }

        if record.exc_text:
            record.exc_text = self._scrubber.scrub_text(record.exc_text)
        return True


def configure_logging(log_level: str, secret_token: str) -> None:
    """Configure structured logging with comprehensive secret redaction."""
    scrubber = get_secret_scrubber()
    scrubber.register(secret_token)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level, logging.INFO))

    redactor = TokenRedactingFilter(secret_token=secret_token, scrubber=scrubber)
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


async def require_http_auth(request: Request) -> None:
    """FastAPI dependency enforcing Bearer token authentication on HTTP routes.

    Implemented as ``async def`` because bearer token verification is pure
    in-memory constant-time comparison; keeping it async prevents FastAPI from
    offloading every authenticated HTTP request to AnyIO's synchronous worker
    thread pool.
    """
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
