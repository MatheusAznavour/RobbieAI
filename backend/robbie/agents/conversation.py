"""Conversational orchestration separating ordinary chat from Windows desktop tasks."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from robbie.agents.runner import TaskRecord, TaskRunner
from robbie.core.security import SecretScrubber, get_secret_scrubber
from robbie.memory.conversations import ChatMessageRecord, ConversationStore
from robbie.providers.config import WorkerModelConfig
from robbie.providers.settings import ProviderSettingsManager

logger = logging.getLogger("robbie.agents.conversation")

ChatMode = Literal["auto", "chat", "desktop"]

ORCHESTRATION_SYSTEM_PROMPT = """\
You are Robbie, a helpful, concise, and safety-conscious Windows desktop AI assistant.

Decide whether the user's latest message is:
1. An ordinary conversation, question, explanation, brainstorming, or code review that does NOT
   require controlling the user's Windows desktop GUI.
2. A request that explicitly asks you to operate the user's Windows desktop (for example: opening an
   application, clicking UI elements, creating/moving/renaming files on the desktop, interacting
   with open windows).

Respond ONLY with a single JSON object (no markdown fences around the JSON) matching one of:
- For ordinary conversation:
  {"intent": "reply", "content": "<your helpful Markdown response>"}
- For a Windows desktop execution request:
  {
    "intent": "desktop_task",
    "instruction": "<clear desktop task instruction>",
    "content": "<brief confirmation of the desktop task>"
  }

Rules:
- Default to {"intent": "reply", ...} whenever the request can be answered directly without GUI
  automation.
- Never claim you performed a desktop action unless you return "desktop_task".
- Never output raw HTML or scripts.
"""

CHAT_ONLY_SYSTEM_PROMPT = """\
You are Robbie, a helpful, concise Windows desktop AI assistant.
Answer the user's message clearly using safe Markdown (no raw HTML or scripts).
Do not claim to have executed actions on the user's computer in chat-only mode.
"""


@dataclass(frozen=True)
class ModelTurnDecision:
    intent: Literal["reply", "desktop_task"]
    content: str
    task_instruction: str | None = None


class ChatModelClientProtocol(Protocol):
    def complete(
        self,
        *,
        model_config: WorkerModelConfig,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> str: ...


class AgentSLmmChatClient:
    """Reuses Agent-S LMMAgent off the asyncio loop without modifying vendor/Agent-S."""

    def complete(
        self,
        *,
        model_config: WorkerModelConfig,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> str:
        from gui_agents.s3.core.mllm import LMMAgent

        engine_params = model_config.to_engine_params()
        lmm = LMMAgent(engine_params=engine_params, system_prompt=system_prompt)
        for msg in messages:
            lmm.add_message(text_content=msg["content"], role=msg["role"])
        response = lmm.get_response(temperature=model_config.temperature or 0.2)
        if not isinstance(response, str) or not response.strip():
            raise RuntimeError("Configured model returned an empty response.")
        return response.strip()


def _parse_orchestration_response(raw_text: str) -> ModelTurnDecision:
    """Parse structured JSON decision from the conversational model safely."""
    cleaned = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    candidate = fence_match.group(1) if fence_match else cleaned

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return ModelTurnDecision(intent="reply", content=cleaned)

    if not isinstance(parsed, dict):
        return ModelTurnDecision(intent="reply", content=cleaned)

    intent = parsed.get("intent")
    content = str(parsed.get("content", "")).strip()
    if intent == "desktop_task":
        instruction = str(parsed.get("instruction", "")).strip()
        if instruction:
            return ModelTurnDecision(
                intent="desktop_task",
                content=content or f"Starting desktop task: {instruction}",
                task_instruction=instruction,
            )

    return ModelTurnDecision(intent="reply", content=content or cleaned)


def build_task_completion_summary(record: TaskRecord) -> str:
    """Generate a truthful conversational summary strictly from actual backend task state."""
    if record.state == "completed":
        return (
            f"Desktop task `{record.task_id[:8]}` completed in {record.current_step} step(s): "
            f"{record.instruction}"
        )
    if record.state == "emergency_stopped":
        return (
            f"Desktop task `{record.task_id[:8]}` was halted by Emergency Stop "
            f"at step {record.current_step}."
        )
    if record.state == "cancelled":
        detail = f" ({record.last_error})" if record.last_error else ""
        return (
            f"Desktop task `{record.task_id[:8]}` was cancelled at step "
            f"{record.current_step}{detail}."
        )
    detail = record.last_error or "Unknown execution error."
    return f"Desktop task `{record.task_id[:8]}` failed at step {record.current_step}: {detail}"


@dataclass(frozen=True)
class SendMessageOutcome:
    conversation_id: str
    user_message: ChatMessageRecord
    assistant_message: ChatMessageRecord
    task: TaskRecord | None = None


class ConversationOrchestrator:
    """Coordinates conversational turns, desktop task dispatch, and honest status summaries."""

    def __init__(
        self,
        *,
        store: ConversationStore,
        settings_manager: ProviderSettingsManager,
        task_runner: TaskRunner,
        broadcaster: Any,
        chat_client: ChatModelClientProtocol | None = None,
        scrubber: SecretScrubber | None = None,
        model_timeout_seconds: float = 90.0,
    ) -> None:
        self._store = store
        self._settings = settings_manager
        self._runner = task_runner
        self._broadcaster = broadcaster
        self._chat_client: ChatModelClientProtocol = chat_client or AgentSLmmChatClient()
        self._scrubber = scrubber or get_secret_scrubber()
        self._model_timeout = model_timeout_seconds

    async def handle_task_terminal_state(self, record: TaskRecord) -> None:
        """Append a truthful assistant summary to the originating conversation on task end."""
        if not record.conversation_id:
            return
        if self._store.get_conversation(record.conversation_id) is None:
            return
        summary = build_task_completion_summary(record)
        msg = self._store.append_message(
            record.conversation_id,
            role="assistant",
            content=summary,
            kind="task_summary",
            task_id=record.task_id,
        )
        await self._broadcaster(
            self._scrubber.scrub_value(
                {
                    "type": "conversation_message",
                    "conversation_id": record.conversation_id,
                    "message": msg.to_dict(),
                }
            )
        )

    async def send_message(
        self,
        conversation_id: str,
        content: str,
        *,
        mode: ChatMode = "auto",
    ) -> SendMessageOutcome:
        conv = self._store.get_conversation(conversation_id)
        if conv is None:
            raise KeyError(f"Conversation '{conversation_id}' not found.")

        cleaned = content.strip()
        if not cleaned:
            raise ValueError("Message content must not be empty.")

        # Validate readiness BEFORE persisting the user message so failed submissions
        # preserve the user's draft in the composer rather than leaving orphan messages.
        if mode == "desktop":
            desktop_issues = self._settings.validate_desktop_readiness()
            if desktop_issues:
                raise RuntimeError(" ".join(desktop_issues))
            runtime_config = self._settings.build_desktop_runtime_config()
            if self._runner.is_emergency_stopped:
                raise RuntimeError(
                    "Emergency stop is active. Reset emergency stop before launching desktop tasks."
                )
            user_msg = self._store.append_message(conversation_id, role="user", content=cleaned)
            task = await self._runner.start_task(
                cleaned,
                runtime_config,
                conversation_id=conversation_id,
            )
            self._store.link_task(conversation_id, task.task_id)
            assistant_msg = self._store.append_message(
                conversation_id,
                role="assistant",
                content=f"Starting Windows desktop task: {cleaned}",
                kind="task_started",
                task_id=task.task_id,
            )
            return SendMessageOutcome(
                conversation_id=conversation_id,
                user_message=user_msg,
                assistant_message=assistant_msg,
                task=task,
            )

        chat_issues = self._settings.validate_chat_readiness()
        if chat_issues:
            raise RuntimeError(" ".join(chat_issues))

        model_config = self._settings.build_chat_model_config()
        if model_config.api_key is not None:
            self._scrubber.register(model_config.api_key.get_secret_value())

        history = [
            {"role": m.role, "content": m.content}
            for m in conv.messages[-20:]
            if m.kind in {"chat", "task_summary"}
        ]
        history.append({"role": "user", "content": cleaned})

        system_prompt = CHAT_ONLY_SYSTEM_PROMPT if mode == "chat" else ORCHESTRATION_SYSTEM_PROMPT

        raw_response = await asyncio.wait_for(
            asyncio.to_thread(
                self._chat_client.complete,
                model_config=model_config,
                system_prompt=system_prompt,
                messages=history,
            ),
            timeout=self._model_timeout,
        )

        if mode == "chat":
            decision = ModelTurnDecision(intent="reply", content=raw_response)
        else:
            decision = _parse_orchestration_response(raw_response)

        if decision.intent == "desktop_task" and decision.task_instruction:
            desktop_issues = self._settings.validate_desktop_readiness()
            if desktop_issues:
                user_msg = self._store.append_message(conversation_id, role="user", content=cleaned)
                assistant_msg = self._store.append_message(
                    conversation_id,
                    role="assistant",
                    content=(
                        "This request requires Windows desktop execution, but desktop execution "
                        f"is not fully configured yet: {' '.join(desktop_issues)}"
                    ),
                    kind="error",
                )
                return SendMessageOutcome(
                    conversation_id=conversation_id,
                    user_message=user_msg,
                    assistant_message=assistant_msg,
                    task=None,
                )

            if self._runner.is_emergency_stopped:
                user_msg = self._store.append_message(conversation_id, role="user", content=cleaned)
                assistant_msg = self._store.append_message(
                    conversation_id,
                    role="assistant",
                    content=(
                        "Cannot start a desktop task while Emergency Stop is active. "
                        "Use Reset Latch once it is safe to proceed."
                    ),
                    kind="error",
                )
                return SendMessageOutcome(
                    conversation_id=conversation_id,
                    user_message=user_msg,
                    assistant_message=assistant_msg,
                    task=None,
                )

            runtime_config = self._settings.build_desktop_runtime_config()
            user_msg = self._store.append_message(conversation_id, role="user", content=cleaned)
            task = await self._runner.start_task(
                decision.task_instruction,
                runtime_config,
                conversation_id=conversation_id,
            )
            self._store.link_task(conversation_id, task.task_id)
            assistant_msg = self._store.append_message(
                conversation_id,
                role="assistant",
                content=decision.content,
                kind="task_started",
                task_id=task.task_id,
            )
            return SendMessageOutcome(
                conversation_id=conversation_id,
                user_message=user_msg,
                assistant_message=assistant_msg,
                task=task,
            )

        user_msg = self._store.append_message(conversation_id, role="user", content=cleaned)
        assistant_msg = self._store.append_message(
            conversation_id,
            role="assistant",
            content=decision.content,
            kind="chat",
        )
        return SendMessageOutcome(
            conversation_id=conversation_id,
            user_message=user_msg,
            assistant_message=assistant_msg,
            task=None,
        )
