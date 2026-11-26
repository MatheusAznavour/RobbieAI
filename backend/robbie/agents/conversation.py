"""Conversational orchestration separating ordinary chat from Windows desktop tasks."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol
from urllib.parse import urlsplit

import httpx
import openai

from robbie.agents.runner import TaskRecord, TaskRunner
from robbie.core.security import SecretScrubber, get_secret_scrubber
from robbie.memory.conversations import ChatMessageRecord, ConversationStore
from robbie.providers.config import WorkerModelConfig
from robbie.providers.settings import ProviderSettingsManager

logger = logging.getLogger("robbie.agents.conversation")

ChatMode = Literal["auto", "chat", "desktop"]

OPENAI_COMPATIBLE_CHAT_PROVIDERS: Final[frozenset[str]] = frozenset(
    {
        "openai",
        "open_router",
        "gemini",
        "deepseek",
        "qwen",
        "vllm",
        "ollama",
        "parasail",
    }
)

DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 40.0
DEFAULT_MAX_RETRIES: Final[int] = 1
DEFAULT_CHAT_MAX_TOKENS: Final[int] = 2048

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


class ProviderRequestError(Exception):
    """Sanitized, structured provider invocation failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class ProviderTimeoutError(ProviderRequestError, TimeoutError):
    """Raised when a model provider request exceeds its bounded timeout."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=504, retryable=True)


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


def _resolve_chat_http_timeout() -> float:
    raw = os.environ.get("ROBBIE_CHAT_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_HTTP_TIMEOUT_SECONDS
    try:
        parsed = float(raw)
    except ValueError:
        return DEFAULT_HTTP_TIMEOUT_SECONDS
    return max(5.0, min(parsed, 120.0))


def _resolve_chat_max_tokens() -> int:
    raw = os.environ.get("ROBBIE_CHAT_MAX_TOKENS", "").strip()
    if not raw:
        return DEFAULT_CHAT_MAX_TOKENS
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_CHAT_MAX_TOKENS
    return max(128, min(parsed, 8192))


def _sanitize_endpoint_for_display(base_url: str | None) -> str:
    """Return scheme://host[:port]/path without userinfo or query parameters."""
    if not base_url:
        return "https://api.openai.com/v1"
    try:
        parts = urlsplit(base_url.strip())
        if not parts.scheme or not parts.hostname:
            return "<configured-endpoint>"
        port_part = f":{parts.port}" if parts.port else ""
        path_part = parts.path.rstrip("/")
        return f"{parts.scheme}://{parts.hostname}{port_part}{path_part}"
    except Exception:
        return "<configured-endpoint>"


def _resolve_openai_compatible_base_url(model_config: WorkerModelConfig) -> str | None:
    base_url = model_config.base_url
    if base_url:
        cleaned = base_url.strip()
        if model_config.provider == "ollama" and not cleaned.endswith("/v1"):
            return cleaned.rstrip("/") + "/v1"
        return cleaned

    if model_config.provider == "openai":
        env_url = os.environ.get("OPENAI_BASE_URL", "").strip()
        return env_url or None
    if model_config.provider == "ollama":
        host = os.environ.get("OLLAMA_HOST", "").strip()
        if not host:
            raise ProviderRequestError(
                "Ollama endpoint URL is not configured (set OLLAMA_HOST or Base URL).",
                status_code=400,
                retryable=False,
            )
        return host.rstrip("/") + ("/v1" if not host.rstrip("/").endswith("/v1") else "")
    if model_config.provider == "deepseek":
        return "https://api.deepseek.com/v1"
    if model_config.provider == "qwen":
        return "https://dashscope.aliyuncs.com/compatible-mode/v1"
    if model_config.provider == "parasail":
        return "https://api.parasail.io/v1"
    if model_config.provider == "gemini":
        return "https://generativelanguage.googleapis.com/v1beta/openai/"
    if model_config.provider == "open_router":
        return "https://openrouter.ai/api/v1"
    return None


def _extract_safe_provider_error_detail(exc: Exception, scrubber: SecretScrubber) -> str:
    body = getattr(exc, "body", None)
    detail = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            detail = err["message"].strip()
        elif isinstance(body.get("message"), str):
            detail = body["message"].strip()
        elif isinstance(body.get("detail"), str):
            detail = body["detail"].strip()
    if not detail:
        detail = str(getattr(exc, "message", "") or str(exc)).strip()
    detail = scrubber.scrub_text(detail)
    if len(detail) > 240:
        detail = detail[:237] + "..."
    return detail


class OpenAICompatibleChatClient:
    """Robbie-owned bounded OpenAI SDK client for conversational text completions.

    Uses explicit httpx timeouts, bounded retries (never retrying deterministic 4xx
    failures), plain-string message content, bounded max_tokens, and scrubbed errors.
    """

    def __init__(
        self,
        *,
        http_timeout_seconds: float | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_tokens: int | None = None,
        scrubber: SecretScrubber | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._http_timeout = (
            float(http_timeout_seconds)
            if http_timeout_seconds is not None
            else _resolve_chat_http_timeout()
        )
        self._max_retries = max(0, min(int(max_retries), 2))
        self._max_tokens = int(max_tokens) if max_tokens is not None else _resolve_chat_max_tokens()
        self._scrubber = scrubber or get_secret_scrubber()
        self._client_factory = client_factory or openai.OpenAI

    @property
    def http_timeout_seconds(self) -> float:
        return self._http_timeout

    @property
    def max_retries(self) -> int:
        return self._max_retries

    def complete(
        self,
        *,
        model_config: WorkerModelConfig,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> str:
        api_key = model_config.api_key.get_secret_value().strip() if model_config.api_key else ""
        if api_key:
            self._scrubber.register(api_key)
        elif model_config.provider == "ollama":
            api_key = "ollama"
        else:
            raise ProviderRequestError(
                f"API key is missing for provider '{model_config.provider}'.",
                status_code=401,
                retryable=False,
            )

        resolved_base_url = _resolve_openai_compatible_base_url(model_config)
        endpoint_display = _sanitize_endpoint_for_display(resolved_base_url)

        timeout_cfg = httpx.Timeout(
            connect=min(10.0, self._http_timeout),
            read=self._http_timeout,
            write=min(30.0, self._http_timeout),
            pool=min(10.0, self._http_timeout),
        )

        client_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": timeout_cfg,
            "max_retries": self._max_retries,
        }
        if resolved_base_url:
            client_kwargs["base_url"] = resolved_base_url
        if model_config.organization:
            client_kwargs["organization"] = model_config.organization

        payload_messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            *({"role": msg["role"], "content": msg["content"]} for msg in messages),
        ]

        temperature = model_config.temperature if model_config.temperature is not None else 0.2

        logger.debug(
            "Sending chat completion request: provider=%s model=%s endpoint=%s "
            "messages=%d max_tokens=%d timeout=%.1fs max_retries=%d",
            model_config.provider,
            model_config.model,
            endpoint_display,
            len(payload_messages),
            self._max_tokens,
            self._http_timeout,
            self._max_retries,
        )

        client = self._client_factory(**client_kwargs)
        try:
            completion = client.chat.completions.create(
                model=model_config.model,
                messages=payload_messages,
                temperature=temperature,
                max_tokens=self._max_tokens,
            )
        except (openai.APITimeoutError, httpx.TimeoutException, TimeoutError) as exc:
            raise ProviderTimeoutError(
                f"Model provider '{model_config.provider}' timed out after "
                f"{self._http_timeout:.0f}s (model='{model_config.model}', "
                f"endpoint='{endpoint_display}')."
            ) from exc
        except openai.AuthenticationError as exc:
            raise ProviderRequestError(
                f"Authentication failed for provider '{model_config.provider}' "
                f"(HTTP 401 at '{endpoint_display}'). Verify your configured API key.",
                status_code=401,
                retryable=False,
            ) from exc
        except openai.PermissionDeniedError as exc:
            raise ProviderRequestError(
                f"Access denied by provider '{model_config.provider}' "
                f"(HTTP 403 at '{endpoint_display}').",
                status_code=403,
                retryable=False,
            ) from exc
        except openai.NotFoundError as exc:
            detail = _extract_safe_provider_error_detail(exc, self._scrubber)
            suffix = f": {detail}" if detail else ""
            raise ProviderRequestError(
                f"Model '{model_config.model}' or endpoint '{endpoint_display}' "
                f"was not found (HTTP 404){suffix}",
                status_code=404,
                retryable=False,
            ) from exc
        except openai.BadRequestError as exc:
            detail = _extract_safe_provider_error_detail(exc, self._scrubber)
            suffix = f": {detail}" if detail else ""
            raise ProviderRequestError(
                f"Provider '{model_config.provider}' rejected the request for model "
                f"'{model_config.model}' (HTTP 400){suffix}",
                status_code=400,
                retryable=False,
            ) from exc
        except openai.RateLimitError as exc:
            raise ProviderRequestError(
                f"Provider '{model_config.provider}' rate limit exceeded (HTTP 429). "
                "Please wait and retry.",
                status_code=429,
                retryable=True,
            ) from exc
        except openai.APIConnectionError as exc:
            raise ProviderRequestError(
                f"Could not connect to provider endpoint '{endpoint_display}'. "
                "Verify the base URL and network connectivity.",
                status_code=502,
                retryable=True,
            ) from exc
        except openai.APIStatusError as exc:
            status_code = getattr(exc, "status_code", 502)
            detail = _extract_safe_provider_error_detail(exc, self._scrubber)
            suffix = f": {detail}" if detail else ""
            raise ProviderRequestError(
                f"Provider '{model_config.provider}' returned HTTP {status_code}{suffix}",
                status_code=status_code,
                retryable=bool(status_code and status_code >= 500),
            ) from exc
        finally:
            close_fn = getattr(client, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass

        choices = getattr(completion, "choices", None)
        if not choices:
            raise ProviderRequestError(
                "Configured model returned no completion choices.",
                status_code=502,
                retryable=False,
            )
        message_obj = getattr(choices[0], "message", None)
        content = getattr(message_obj, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise ProviderRequestError(
                "Configured model returned an empty response.",
                status_code=502,
                retryable=False,
            )
        return content.strip()


class AgentSLmmChatClient:
    """Conversational model client routing OpenAI-compatible providers through Robbie's
    bounded OpenAI SDK client and falling back to Agent-S LMMAgent for other providers.
    """

    def __init__(
        self,
        *,
        openai_client: OpenAICompatibleChatClient | None = None,
        http_timeout_seconds: float | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_tokens: int | None = None,
        scrubber: SecretScrubber | None = None,
    ) -> None:
        self._openai_client = openai_client or OpenAICompatibleChatClient(
            http_timeout_seconds=http_timeout_seconds,
            max_retries=max_retries,
            max_tokens=max_tokens,
            scrubber=scrubber,
        )

    @property
    def recommended_async_timeout_seconds(self) -> float:
        """Return an outer asyncio timeout larger than the bounded HTTP retry budget."""
        budget = self._openai_client.http_timeout_seconds * (self._openai_client.max_retries + 1)
        return max(budget + 10.0, 90.0)

    def complete(
        self,
        *,
        model_config: WorkerModelConfig,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> str:
        if model_config.provider in OPENAI_COMPATIBLE_CHAT_PROVIDERS:
            return self._openai_client.complete(
                model_config=model_config,
                system_prompt=system_prompt,
                messages=messages,
            )

        from gui_agents.s3.core.mllm import LMMAgent

        engine_params = model_config.to_engine_params()
        lmm = LMMAgent(engine_params=engine_params, system_prompt=system_prompt)
        for msg in messages:
            lmm.add_message(text_content=msg["content"], role=msg["role"])
        response = lmm.get_response(
            temperature=model_config.temperature if model_config.temperature is not None else 0.2
        )
        if not isinstance(response, str) or not response.strip():
            raise ProviderRequestError(
                "Configured model returned an empty response.",
                status_code=502,
                retryable=False,
            )
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
        model_timeout_seconds: float | None = None,
    ) -> None:
        self._store = store
        self._settings = settings_manager
        self._runner = task_runner
        self._broadcaster = broadcaster
        self._scrubber = scrubber or get_secret_scrubber()
        self._chat_client: ChatModelClientProtocol = chat_client or AgentSLmmChatClient(
            scrubber=self._scrubber
        )
        if model_timeout_seconds is not None:
            self._model_timeout = float(model_timeout_seconds)
        elif isinstance(self._chat_client, AgentSLmmChatClient):
            self._model_timeout = self._chat_client.recommended_async_timeout_seconds
        else:
            self._model_timeout = 90.0

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

        # Note: Cancelling asyncio.to_thread does NOT forcibly interrupt an in-flight
        # synchronous HTTP socket call; OpenAICompatibleChatClient enforces explicit
        # httpx timeouts inside the worker thread so sockets close before this guard.
        try:
            raw_response = await asyncio.wait_for(
                asyncio.to_thread(
                    self._chat_client.complete,
                    model_config=model_config,
                    system_prompt=system_prompt,
                    messages=history,
                ),
                timeout=self._model_timeout,
            )
        except ProviderTimeoutError:
            raise
        except TimeoutError as exc:
            endpoint_display = _sanitize_endpoint_for_display(model_config.base_url)
            raise ProviderTimeoutError(
                f"Model provider '{model_config.provider}' timed out after "
                f"{self._model_timeout:.0f}s (model='{model_config.model}', "
                f"endpoint='{endpoint_display}')."
            ) from exc

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
