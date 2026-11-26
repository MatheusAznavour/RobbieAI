"""Automated tests for Robbie backend configuration, auth, health, and WebSocket routes."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from starlette.websockets import WebSocketDisconnect

import robbie
from robbie.core.config import BackendSettings
from robbie.core.security import TokenRedactingFilter
from robbie.main import create_app

TEST_TOKEN = "test-session-token-with-sufficient-entropy-12345"
TRUSTED_ORIGIN = "robbie://desktop"


def _make_settings(**overrides: object) -> BackendSettings:
    defaults: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 0,
        "api_token": SecretStr(TEST_TOKEN),
        "allowed_origins": (TRUSTED_ORIGIN,),
        "log_level": "INFO",
    }
    defaults.update(overrides)
    return BackendSettings(**defaults)


def test_settings_enforce_loopback_host() -> None:
    with pytest.raises(ValidationError, match="127.0.0.1"):
        _make_settings(host="0.0.0.0")

    with pytest.raises(ValidationError, match="127.0.0.1"):
        _make_settings(host="localhost")


def test_settings_reject_wildcard_origin_and_short_token() -> None:
    with pytest.raises(ValidationError, match="Wildcard"):
        _make_settings(allowed_origins=("*",))

    with pytest.raises(ValidationError, match="at least 16"):
        _make_settings(api_token=SecretStr("short"))


def test_settings_mask_token_and_redacting_filter_scrubs_logs() -> None:
    settings = _make_settings()
    assert TEST_TOKEN not in repr(settings)
    assert TEST_TOKEN not in str(settings)

    log_filter = TokenRedactingFilter(TEST_TOKEN)
    record = logging.LogRecord(
        name="robbie.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Header was Bearer %s",
        args=(TEST_TOKEN,),
        exc_info=None,
    )
    assert log_filter.filter(record) is True
    assert TEST_TOKEN not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()


def test_health_requires_valid_bearer_token() -> None:
    app = create_app(_make_settings())
    with TestClient(app) as client:
        # Missing header
        missing = client.get("/health")
        assert missing.status_code == 401
        assert missing.headers.get("www-authenticate") == "Bearer"

        # Wrong scheme
        wrong_scheme = client.get("/health", headers={"Authorization": f"Basic {TEST_TOKEN}"})
        assert wrong_scheme.status_code == 401

        # Wrong token
        wrong_token = client.get("/health", headers={"Authorization": "Bearer wrong-token-value"})
        assert wrong_token.status_code == 401

        # OpenAPI/docs disabled on local sidecar
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_health_returns_runtime_metadata_with_valid_auth() -> None:
    app = create_app(_make_settings())
    with TestClient(app) as client:
        response = client.get("/health", headers={"Authorization": f"Bearer {TEST_TOKEN}"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["service"] == "robbie-backend"
        assert payload["version"] == robbie.__version__
        assert payload["host"] == "127.0.0.1"
        assert isinstance(payload["pid"], int)
        assert payload["uptime_seconds"] >= 0.0
        assert payload["websocket_connections"] == 0
        assert isinstance(payload["timestamp"], str)


def test_websocket_rejects_missing_or_untrusted_origin() -> None:
    app = create_app(_make_settings())
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/ws/status",
                headers={"Authorization": f"Bearer {TEST_TOKEN}"},
            ):
                pass

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/ws/status",
                headers={
                    "Authorization": f"Bearer {TEST_TOKEN}",
                    "Origin": "https://evil.example",
                },
            ):
                pass


def test_websocket_rejects_missing_or_invalid_token() -> None:
    app = create_app(_make_settings())
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/ws/status",
                headers={"Origin": TRUSTED_ORIGIN},
            ):
                pass

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/ws/status",
                headers={
                    "Origin": TRUSTED_ORIGIN,
                    "Authorization": "Bearer invalid-token-123456",
                },
            ):
                pass


def test_websocket_authenticated_flow_and_invalid_messages() -> None:
    app = create_app(_make_settings())
    headers = {
        "Origin": TRUSTED_ORIGIN,
        "Authorization": f"Bearer {TEST_TOKEN}",
    }
    with TestClient(app) as client:
        with client.websocket_connect("/ws/status", headers=headers) as ws:
            initial = ws.receive_json()
            assert initial["type"] == "status"
            assert initial["status"] == "ready"
            assert initial["version"] == robbie.__version__
            assert initial["host"] == "127.0.0.1"

            # Verify live connection count via HTTP /health
            health = client.get("/health", headers={"Authorization": f"Bearer {TEST_TOKEN}"})
            assert health.status_code == 200
            assert health.json()["websocket_connections"] == 1

            # Ping -> Pong
            ws.send_json({"type": "ping", "request_id": "ping-42"})
            pong = ws.receive_json()
            assert pong["type"] == "pong"
            assert pong["request_id"] == "ping-42"
            assert isinstance(pong["timestamp"], str)

            # Explicit status request
            ws.send_json({"type": "status_request"})
            status_msg = ws.receive_json()
            assert status_msg["type"] == "status"
            assert status_msg["status"] == "ready"

            # Invalid JSON
            ws.send_text("not-valid-json")
            err_json = ws.receive_json()
            assert err_json["type"] == "error"
            assert err_json["code"] == "invalid_json"

            # Non-object JSON
            ws.send_text("[1, 2, 3]")
            err_payload = ws.receive_json()
            assert err_payload["type"] == "error"
            assert err_payload["code"] == "invalid_payload"

            # Unsupported message type
            ws.send_json({"type": "unknown_command"})
            err_type = ws.receive_json()
            assert err_type["type"] == "error"
            assert err_type["code"] == "unsupported_message_type"

        # Disconnected count should return to zero
        health_after = client.get("/health", headers={"Authorization": f"Bearer {TEST_TOKEN}"})
        assert health_after.status_code == 200
        assert health_after.json()["websocket_connections"] == 0
