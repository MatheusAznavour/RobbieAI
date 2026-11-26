"""Tests for Milestone 3 conversation persistence, chat/desktop orchestration, and settings."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from robbie.agents.runner import TaskRunner
from robbie.core.config import BackendSettings
from robbie.main import create_app
from robbie.memory.conversations import ConversationStore
from robbie.providers.settings import ProviderSettingsManager
from robbie.tools.actions import ClickAction, DoneAction
from tests.test_task_runner_and_api import (
    AUTH_HEADERS,
    TEST_TOKEN,
    TRUSTED_ORIGIN,
    FakeScreenCapture,
    RecordingDispatcher,
    ScriptedAdapter,
)


class FakeChatClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        model_config: Any,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> str:
        self.calls.append({"model": model_config.model, "messages": list(messages)})
        return self._responses.pop(0)


def _make_m3_app(
    tmp_path: Path,
    *,
    chat_responses: list[str] | None = None,
    adapter: ScriptedAdapter | None = None,
    dispatcher: RecordingDispatcher | None = None,
):
    settings = BackendSettings(
        host="127.0.0.1",
        port=0,
        api_token=SecretStr(TEST_TOKEN),
        allowed_origins=(TRUSTED_ORIGIN,),
    )
    store = ConversationStore(storage_path=tmp_path / "conversations.json", max_conversations=5)
    settings_mgr = ProviderSettingsManager(storage_path=tmp_path / "settings.json")
    chat_client = FakeChatClient(chat_responses or [])

    app = create_app(
        settings,
        conversation_store=store,
        settings_manager=settings_mgr,
        chat_client=chat_client,
    )
    if adapter is not None:
        custom_runner = TaskRunner(
            broadcaster=app.state.runtime.connections.broadcast_json,
            adapter_factory=lambda _cfg: adapter,
            dispatcher_factory=lambda _cfg: dispatcher or RecordingDispatcher(),
            screen_capture_factory=lambda: FakeScreenCapture(),
            on_task_finished=app.state.runtime.orchestrator.handle_task_terminal_state,
        )
        app.state.runtime.task_runner = custom_runner
        app.state.runtime.orchestrator._runner = custom_runner
    return app, store, chat_client


def test_conversation_persistence_bounds_search_rename_delete_and_scrubbing(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "conversations.json"
    store = ConversationStore(storage_path=storage, max_conversations=3)

    c1 = store.create_conversation()
    store.append_message(
        c1.conversation_id,
        role="user",
        content="Explain decorators with key sk-proj-supersecret123456789",
    )
    assert "sk-proj-supersecret123456789" not in c1.title
    assert "[REDACTED]" in c1.title

    store.rename_conversation(c1.conversation_id, "Python Decorators")
    assert store.get_conversation(c1.conversation_id).title == "Python Decorators"  # type: ignore[union-attr]

    # Reload from disk proves persistence and confirms secret never touched disk
    raw_disk = storage.read_text(encoding="utf-8")
    assert "sk-proj-supersecret123456789" not in raw_disk
    reloaded = ConversationStore(storage_path=storage, max_conversations=3)
    assert len(reloaded.list_conversations(query="decorators")) == 1

    # Bounded storage evicts oldest conversations cleanly
    for i in range(4):
        reloaded.create_conversation(title=f"Chat {i}")
    assert len(reloaded.list_conversations()) <= 3

    target_id = reloaded.list_conversations()[0]["conversation_id"]
    reloaded.delete_conversation(target_id)
    assert reloaded.get_conversation(target_id) is None


def test_ordinary_conversation_answers_without_launching_agent_s(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-backend-only-secret-12345")
    adapter = ScriptedAdapter([DoneAction()])
    reply_json = json.dumps(
        {
            "intent": "reply",
            "content": "A Python decorator wraps a function to extend its behavior.",
        }
    )
    app, _store, chat_client = _make_m3_app(
        tmp_path,
        chat_responses=[reply_json],
        adapter=adapter,
    )

    with TestClient(app) as client:
        # Endpoints require auth
        assert client.get("/conversations").status_code == 401
        assert client.get("/settings").status_code == 401

        conv_resp = client.post("/conversations", headers=AUTH_HEADERS, json={})
        assert conv_resp.status_code == 201
        conv_id = conv_resp.json()["conversation_id"]

        msg_resp = client.post(
            f"/conversations/{conv_id}/messages",
            headers=AUTH_HEADERS,
            json={"content": "Explain how Python decorators work.", "mode": "auto"},
        )
        assert msg_resp.status_code == 200
        body = msg_resp.json()
        assert body["task"] is None
        assert "decorator" in body["assistant_message"]["content"].lower()
        assert body["conversation"]["title"] == "Explain how Python decorators work."
        # Agent-S adapter was NEVER invoked for ordinary conversation
        assert adapter.calls == 0
        assert len(chat_client.calls) == 1


def test_desktop_request_in_chat_launches_agent_s_and_appends_truthful_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-backend-only-secret-12345")
    monkeypatch.setenv("HF_TOKEN", "hf_backendonlysecret1234567890")
    monkeypatch.setenv("ROBBIE_GROUNDING_URL", "http://127.0.0.1:8080")

    adapter = ScriptedAdapter(
        [
            ClickAction(x=100, y=100, element_description="Click the Help menu tab"),
            DoneAction(),
        ]
    )
    dispatcher = RecordingDispatcher()
    intent_json = json.dumps(
        {
            "intent": "desktop_task",
            "instruction": "Open the Help menu tab",
            "content": "Starting a desktop task to open the Help menu tab.",
        }
    )
    app, _store, _chat = _make_m3_app(
        tmp_path,
        chat_responses=[intent_json],
        adapter=adapter,
        dispatcher=dispatcher,
    )

    with TestClient(app) as client:
        conv_id = client.post("/conversations", headers=AUTH_HEADERS, json={}).json()[
            "conversation_id"
        ]
        send_resp = client.post(
            f"/conversations/{conv_id}/messages",
            headers=AUTH_HEADERS,
            json={"content": "Open the Help menu tab on my desktop", "mode": "auto"},
        )
        assert send_resp.status_code == 200
        data = send_resp.json()
        assert data["task"] is not None
        task_id = data["task"]["task_id"]
        assert data["task"]["conversation_id"] == conv_id

        # Wait for task completion and truthful assistant summary
        deadline = time.monotonic() + 5.0
        detail: dict[str, Any] = {}
        while time.monotonic() < deadline:
            detail = client.get(f"/conversations/{conv_id}", headers=AUTH_HEADERS).json()
            if any(m["kind"] == "task_summary" for m in detail["messages"]):
                break
            time.sleep(0.02)

        summaries = [m for m in detail["messages"] if m["kind"] == "task_summary"]
        assert len(summaries) == 1
        assert "completed" in summaries[0]["content"].lower()
        assert summaries[0]["task_id"] == task_id
        assert len(dispatcher.executed) == 1


def test_settings_never_expose_api_keys_and_forbid_safety_disable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_key = "sk-never-send-this-to-renderer-99999"
    monkeypatch.setenv("OPENAI_API_KEY", secret_key)
    app, _store, _chat = _make_m3_app(tmp_path)

    with TestClient(app) as client:
        resp = client.get("/settings", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        text = resp.text
        assert secret_key not in text
        body = resp.json()
        assert body["safety_policy_locked"] is True
        openai_entry = next(p for p in body["providers"] if p["id"] == "openai")
        assert openai_entry["configured"] is True

        # Update model selection cleanly
        updated_sel = dict(body["selection"])
        updated_sel["chat_model"] = "gpt-4o"
        put_resp = client.put(
            "/settings",
            headers=AUTH_HEADERS,
            json={"selection": updated_sel},
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["selection"]["chat_model"] == "gpt-4o"

        # Attempting to inject safety-disable field is rejected (422)
        bad_sel = dict(updated_sel)
        bad_sel["disable_safety"] = True
        assert (
            client.put("/settings", headers=AUTH_HEADERS, json={"selection": bad_sel}).status_code
            == 422
        )

        # /health reflects persistent emergency_stopped state
        assert client.get("/health", headers=AUTH_HEADERS).json()["emergency_stopped"] is False
        assert client.post("/emergency-stop", headers=AUTH_HEADERS).status_code == 200
        assert client.get("/health", headers=AUTH_HEADERS).json()["emergency_stopped"] is True
        assert client.post("/emergency-stop/reset", headers=AUTH_HEADERS).status_code == 200
        assert client.get("/health", headers=AUTH_HEADERS).json()["emergency_stopped"] is False
