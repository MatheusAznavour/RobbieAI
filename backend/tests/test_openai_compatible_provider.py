"""Regression tests for M3.1 OpenAI-compatible provider integration and timeout handling."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from robbie.agents.conversation import (
    AgentSLmmChatClient,
    ConversationOrchestrator,
    OpenAICompatibleChatClient,
)
from robbie.core.config import BackendSettings
from robbie.core.security import SecretScrubber
from robbie.main import create_app
from robbie.memory.conversations import ConversationStore
from robbie.providers.config import WorkerModelConfig
from robbie.providers.settings import DEFAULT_OPENAI_MODEL, ProviderSettingsManager
from tests.test_task_runner_and_api import AUTH_HEADERS, TEST_TOKEN, TRUSTED_ORIGIN

UNCENSORED_BASE_URL = "https://api.uncensoredllm.ai/v1"
UNCENSORED_MODEL = "glm-5.3-flash-uncensored"
FAKE_API_KEY = "sk-uncensored-test-secret-do-not-leak-987654"


def _clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "ROBBIE_WORKER_PROVIDER",
        "ROBBIE_WORKER_MODEL",
        "ROBBIE_WORKER_BASE_URL",
        "ROBBIE_WORKER_TEMPERATURE",
        "ROBBIE_CHAT_PROVIDER",
        "ROBBIE_CHAT_MODEL",
        "ROBBIE_CHAT_BASE_URL",
        "ROBBIE_CHAT_TIMEOUT_SECONDS",
        "ROBBIE_CHAT_MAX_TOKENS",
    ):
        monkeypatch.delenv(var, raising=False)


class _RecordingOpenAIClient:
    """Mock OpenAI SDK client capturing constructor kwargs and chat.completions.create payloads."""

    def __init__(
        self,
        *,
        init_kwargs: dict[str, Any],
        records: list[dict[str, Any]],
        behavior: Any,
    ) -> None:
        self._init_kwargs = init_kwargs
        self._records = records
        self._behavior = behavior
        self.closed = False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self._records.append({"init": dict(self._init_kwargs), "create": dict(kwargs)})
        if callable(self._behavior):
            return self._behavior(**kwargs)
        if isinstance(self._behavior, BaseException):
            raise self._behavior
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=str(self._behavior)))]
        )

    def close(self) -> None:
        self.closed = True


def _make_recording_factory(records: list[dict[str, Any]], behavior: Any):
    def _factory(**kwargs: Any) -> _RecordingOpenAIClient:
        return _RecordingOpenAIClient(init_kwargs=kwargs, records=records, behavior=behavior)

    return _factory


def _make_http_response(status_code: int, body: dict[str, Any]) -> httpx.Response:
    request = httpx.Request("POST", f"{UNCENSORED_BASE_URL}/chat/completions")
    return httpx.Response(status_code=status_code, json=body, request=request)


def _make_app_with_client(
    tmp_path: Path,
    chat_client: Any,
    *,
    model_timeout_seconds: float | None = None,
):
    settings = BackendSettings(
        host="127.0.0.1",
        port=0,
        api_token=SecretStr(TEST_TOKEN),
        allowed_origins=(TRUSTED_ORIGIN,),
    )
    store = ConversationStore(storage_path=tmp_path / "conversations.json")
    settings_mgr = ProviderSettingsManager(storage_path=tmp_path / "settings.json")
    app = create_app(
        settings,
        conversation_store=store,
        settings_manager=settings_mgr,
        chat_client=chat_client,
    )
    if model_timeout_seconds is not None:
        app.state.runtime.orchestrator = ConversationOrchestrator(
            store=store,
            settings_manager=settings_mgr,
            task_runner=app.state.runtime.task_runner,
            broadcaster=app.state.runtime.connections.broadcast_json,
            chat_client=chat_client,
            model_timeout_seconds=model_timeout_seconds,
        )
    return app, settings_mgr


# ---------------------------------------------------------------------------
# 1. Provider settings precedence & base URL resolution
# ---------------------------------------------------------------------------


def test_uncensoredllm_base_url_and_worker_env_inheritance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("ROBBIE_WORKER_PROVIDER", "openai")
    monkeypatch.setenv("ROBBIE_WORKER_MODEL", UNCENSORED_MODEL)
    monkeypatch.setenv("ROBBIE_WORKER_BASE_URL", UNCENSORED_BASE_URL)
    monkeypatch.setenv("ROBBIE_WORKER_TEMPERATURE", "1.0")

    mgr = ProviderSettingsManager(storage_path=tmp_path / "settings.json")
    sel = mgr.get_selection()
    assert sel.chat_provider == "openai"
    assert sel.chat_model == UNCENSORED_MODEL
    assert sel.chat_base_url == UNCENSORED_BASE_URL
    assert sel.worker_model == UNCENSORED_MODEL
    assert sel.worker_base_url == UNCENSORED_BASE_URL

    chat_cfg = mgr.build_chat_model_config()
    assert chat_cfg.provider == "openai"
    assert chat_cfg.model == UNCENSORED_MODEL
    assert chat_cfg.base_url == UNCENSORED_BASE_URL
    assert chat_cfg.temperature == 1.0
    assert chat_cfg.api_key is not None
    assert chat_cfg.api_key.get_secret_value() == FAKE_API_KEY


def test_openai_base_url_env_var_is_resolved_for_openai_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("OPENAI_BASE_URL", UNCENSORED_BASE_URL)
    monkeypatch.setenv("ROBBIE_WORKER_MODEL", UNCENSORED_MODEL)

    mgr = ProviderSettingsManager(storage_path=tmp_path / "settings.json")
    snapshot = mgr.get_public_snapshot()
    openai_meta = next(p for p in snapshot["providers"] if p["id"] == "openai")
    assert openai_meta["endpoint_env_var"] == "OPENAI_BASE_URL"
    assert snapshot["selection"]["chat_base_url"] == UNCENSORED_BASE_URL

    chat_cfg = mgr.build_chat_model_config()
    assert chat_cfg.base_url == UNCENSORED_BASE_URL
    assert chat_cfg.model == UNCENSORED_MODEL


def test_persisted_null_and_stale_default_values_do_not_erase_env_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("ROBBIE_WORKER_PROVIDER", "openai")
    monkeypatch.setenv("ROBBIE_WORKER_MODEL", UNCENSORED_MODEL)
    monkeypatch.setenv("ROBBIE_WORKER_BASE_URL", UNCENSORED_BASE_URL)
    monkeypatch.setenv("ROBBIE_WORKER_TEMPERATURE", "1.0")

    # Simulate stale settings.json captured before .env configured UncensoredLLM
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "chat_provider": "openai",
                "chat_model": DEFAULT_OPENAI_MODEL,
                "chat_base_url": None,
                "worker_provider": "openai",
                "worker_model": DEFAULT_OPENAI_MODEL,
                "worker_base_url": "",
                "grounding_provider": "huggingface",
                "grounding_model": "ui-tars-1.5-7b",
                "grounding_url": "",
                "max_steps": 12,
                "enable_reflection": False,
            }
        ),
        encoding="utf-8",
    )

    mgr = ProviderSettingsManager(storage_path=settings_path)
    sel = mgr.get_selection()
    # Env model & base URL survive stale null/default entries
    assert sel.chat_model == UNCENSORED_MODEL
    assert sel.chat_base_url == UNCENSORED_BASE_URL
    assert sel.worker_model == UNCENSORED_MODEL
    assert sel.worker_base_url == UNCENSORED_BASE_URL
    # Explicit non-default user preferences still persist
    assert sel.max_steps == 12
    assert sel.enable_reflection is False


def test_explicit_user_settings_remain_persistent_across_reloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("ROBBIE_WORKER_MODEL", UNCENSORED_MODEL)
    monkeypatch.setenv("ROBBIE_WORKER_BASE_URL", UNCENSORED_BASE_URL)

    settings_path = tmp_path / "settings.json"
    mgr1 = ProviderSettingsManager(storage_path=settings_path)
    updated = mgr1.get_selection().model_copy(
        update={
            "chat_model": "custom-user-selected-model",
            "chat_base_url": "https://custom.gateway.example/v1",
            "max_steps": 25,
        }
    )
    mgr1.update_selection(updated)

    mgr2 = ProviderSettingsManager(storage_path=settings_path)
    reloaded = mgr2.get_selection()
    assert reloaded.chat_model == "custom-user-selected-model"
    assert reloaded.chat_base_url == "https://custom.gateway.example/v1"
    assert reloaded.max_steps == 25


# ---------------------------------------------------------------------------
# 2. Robbie-owned OpenAI-compatible client payload & security
# ---------------------------------------------------------------------------


def test_openai_compatible_client_sends_plain_string_messages_and_bounded_config(
    caplog: pytest.LogCaptureFixture,
) -> None:
    records: list[dict[str, Any]] = []
    scrubber = SecretScrubber()
    reply = json.dumps({"intent": "reply", "content": "Hello from UncensoredLLM!"})
    client = OpenAICompatibleChatClient(
        http_timeout_seconds=30.0,
        max_retries=1,
        max_tokens=1024,
        scrubber=scrubber,
        client_factory=_make_recording_factory(records, reply),
    )

    cfg = WorkerModelConfig(
        provider="openai",
        model=UNCENSORED_MODEL,
        base_url=UNCENSORED_BASE_URL,
        api_key=SecretStr(FAKE_API_KEY),
        temperature=1.0,
    )

    with caplog.at_level(logging.DEBUG, logger="robbie.agents.conversation"):
        result = client.complete(
            model_config=cfg,
            system_prompt="You are Robbie.",
            messages=[
                {"role": "user", "content": "First question"},
                {"role": "assistant", "content": "First answer"},
                {"role": "user", "content": "Second question"},
            ],
        )

    assert result == reply
    assert len(records) == 1
    init_kwargs = records[0]["init"]
    create_kwargs = records[0]["create"]

    # Explicit base_url, bounded httpx.Timeout, bounded retries
    assert init_kwargs["base_url"] == UNCENSORED_BASE_URL
    assert init_kwargs["api_key"] == FAKE_API_KEY
    assert init_kwargs["max_retries"] == 1
    assert isinstance(init_kwargs["timeout"], httpx.Timeout)
    assert init_kwargs["timeout"].read == 30.0
    assert init_kwargs["timeout"].connect == 10.0

    # Plain-string messages preserving system prompt and full conversation history
    assert create_kwargs["model"] == UNCENSORED_MODEL
    assert create_kwargs["temperature"] == 1.0
    assert create_kwargs["max_tokens"] == 1024
    assert create_kwargs["messages"] == [
        {"role": "system", "content": "You are Robbie."},
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "First answer"},
        {"role": "user", "content": "Second question"},
    ]
    assert all(isinstance(m["content"], str) for m in create_kwargs["messages"])

    # API key never logged
    assert FAKE_API_KEY not in caplog.text


# ---------------------------------------------------------------------------
# 3. Error handling (auth, invalid model, timeout) & conversation history
# ---------------------------------------------------------------------------


def test_provider_authentication_error_fails_fast_without_leaking_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("ROBBIE_WORKER_MODEL", UNCENSORED_MODEL)
    monkeypatch.setenv("ROBBIE_WORKER_BASE_URL", UNCENSORED_BASE_URL)

    records: list[dict[str, Any]] = []
    auth_err = openai.AuthenticationError(
        message=f"Invalid API key provided: {FAKE_API_KEY}",
        response=_make_http_response(401, {"error": {"message": f"Invalid key {FAKE_API_KEY}"}}),
        body={"error": {"message": f"Invalid key {FAKE_API_KEY}"}},
    )
    chat_client = AgentSLmmChatClient(
        openai_client=OpenAICompatibleChatClient(
            http_timeout_seconds=15.0,
            max_retries=1,
            client_factory=_make_recording_factory(records, auth_err),
        )
    )
    app, _mgr = _make_app_with_client(tmp_path, chat_client)

    with caplog.at_level(logging.ERROR), TestClient(app) as client:
        # Settings endpoint never exposes API key
        settings_resp = client.get("/settings", headers=AUTH_HEADERS)
        assert settings_resp.status_code == 200
        assert FAKE_API_KEY not in settings_resp.text

        conv_id = client.post("/conversations", headers=AUTH_HEADERS, json={}).json()[
            "conversation_id"
        ]
        resp = client.post(
            f"/conversations/{conv_id}/messages",
            headers=AUTH_HEADERS,
            json={"content": "Hello", "mode": "chat"},
        )
        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert "401" in detail or "Authentication failed" in detail
        assert FAKE_API_KEY not in detail
        assert FAKE_API_KEY not in caplog.text
        # Deterministic auth failure invokes client once (no retry loop)
        assert len(records) == 1
        # Failed message does not leave orphan user message in conversation
        conv_detail = client.get(f"/conversations/{conv_id}", headers=AUTH_HEADERS).json()
        assert conv_detail["messages"] == []


def test_provider_invalid_model_error_returns_clear_sanitized_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("ROBBIE_WORKER_MODEL", "nonexistent-model-xyz")
    monkeypatch.setenv("ROBBIE_WORKER_BASE_URL", UNCENSORED_BASE_URL)

    records: list[dict[str, Any]] = []
    not_found_err = openai.NotFoundError(
        message="The model `nonexistent-model-xyz` does not exist",
        response=_make_http_response(
            404, {"error": {"message": "The model `nonexistent-model-xyz` does not exist"}}
        ),
        body={"error": {"message": "The model `nonexistent-model-xyz` does not exist"}},
    )
    chat_client = AgentSLmmChatClient(
        openai_client=OpenAICompatibleChatClient(
            http_timeout_seconds=15.0,
            max_retries=1,
            client_factory=_make_recording_factory(records, not_found_err),
        )
    )
    app, _mgr = _make_app_with_client(tmp_path, chat_client)

    with TestClient(app) as client:
        conv_id = client.post("/conversations", headers=AUTH_HEADERS, json={}).json()[
            "conversation_id"
        ]
        resp = client.post(
            f"/conversations/{conv_id}/messages",
            headers=AUTH_HEADERS,
            json={"content": "Hello", "mode": "chat"},
        )
        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert "nonexistent-model-xyz" in detail
        assert "404" in detail
        assert FAKE_API_KEY not in detail
        assert len(records) == 1


def test_provider_sdk_timeout_returns_504_with_clear_sanitized_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("ROBBIE_WORKER_MODEL", UNCENSORED_MODEL)
    monkeypatch.setenv("ROBBIE_WORKER_BASE_URL", UNCENSORED_BASE_URL)

    records: list[dict[str, Any]] = []
    req = httpx.Request("POST", f"{UNCENSORED_BASE_URL}/chat/completions")
    timeout_err = openai.APITimeoutError(request=req)
    chat_client = AgentSLmmChatClient(
        openai_client=OpenAICompatibleChatClient(
            http_timeout_seconds=12.0,
            max_retries=0,
            client_factory=_make_recording_factory(records, timeout_err),
        )
    )
    app, _mgr = _make_app_with_client(tmp_path, chat_client)

    with TestClient(app) as client:
        conv_id = client.post("/conversations", headers=AUTH_HEADERS, json={}).json()[
            "conversation_id"
        ]
        resp = client.post(
            f"/conversations/{conv_id}/messages",
            headers=AUTH_HEADERS,
            json={"content": "Hello", "mode": "chat"},
        )
        assert resp.status_code == 504
        detail = resp.json()["detail"]
        assert "timed out" in detail.lower()
        assert UNCENSORED_MODEL in detail
        assert UNCENSORED_BASE_URL in detail
        assert FAKE_API_KEY not in detail


def test_orchestrator_async_timeout_guard_returns_504_never_bare_timeouterror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("ROBBIE_WORKER_MODEL", UNCENSORED_MODEL)
    monkeypatch.setenv("ROBBIE_WORKER_BASE_URL", UNCENSORED_BASE_URL)

    class _SlowChatClient:
        def complete(
            self,
            *,
            model_config: Any,
            system_prompt: str,
            messages: list[dict[str, str]],
        ) -> str:
            time.sleep(0.35)
            return "too late"

    app, _mgr = _make_app_with_client(
        tmp_path,
        _SlowChatClient(),
        model_timeout_seconds=0.05,
    )

    with TestClient(app) as client:
        conv_id = client.post("/conversations", headers=AUTH_HEADERS, json={}).json()[
            "conversation_id"
        ]
        resp = client.post(
            f"/conversations/{conv_id}/messages",
            headers=AUTH_HEADERS,
            json={"content": "Hello", "mode": "chat"},
        )
        assert resp.status_code == 504
        detail = resp.json()["detail"]
        assert detail != "Model invocation failed: TimeoutError"
        assert "timed out" in detail.lower()
        assert UNCENSORED_MODEL in detail
        assert FAKE_API_KEY not in detail


def test_multi_turn_conversation_history_is_preserved_across_turns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("ROBBIE_WORKER_MODEL", UNCENSORED_MODEL)
    monkeypatch.setenv("ROBBIE_WORKER_BASE_URL", UNCENSORED_BASE_URL)

    records: list[dict[str, Any]] = []
    answers = iter(["Hi! Nice to meet you.", "Your favorite color is cobalt."])

    def _responder(**_kwargs: Any) -> Any:
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=next(answers)))]
        )

    chat_client = AgentSLmmChatClient(
        openai_client=OpenAICompatibleChatClient(
            http_timeout_seconds=20.0,
            max_retries=1,
            client_factory=_make_recording_factory(records, _responder),
        )
    )
    app, _mgr = _make_app_with_client(tmp_path, chat_client)

    with TestClient(app) as client:
        conv_id = client.post("/conversations", headers=AUTH_HEADERS, json={}).json()[
            "conversation_id"
        ]

        r1 = client.post(
            f"/conversations/{conv_id}/messages",
            headers=AUTH_HEADERS,
            json={"content": "My favorite color is cobalt.", "mode": "chat"},
        )
        assert r1.status_code == 200

        r2 = client.post(
            f"/conversations/{conv_id}/messages",
            headers=AUTH_HEADERS,
            json={"content": "What is my favorite color?", "mode": "chat"},
        )
        assert r2.status_code == 200
        assert "cobalt" in r2.json()["assistant_message"]["content"].lower()

        # Second turn receives system prompt + prior user/assistant messages + latest user prompt
        assert len(records) == 2
        second_messages = records[1]["create"]["messages"]
        assert [m["role"] for m in second_messages] == [
            "system",
            "user",
            "assistant",
            "user",
        ]
        assert second_messages[1]["content"] == "My favorite color is cobalt."
        assert second_messages[2]["content"] == "Hi! Nice to meet you."
        assert second_messages[3]["content"] == "What is my favorite color?"
