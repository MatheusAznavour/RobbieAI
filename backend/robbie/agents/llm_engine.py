"""Robbie-owned bounded OpenAI-compatible LMMEngine for Agent-S desktop execution.

Replaces upstream LMMEngineOpenAI's unbounded 600s read timeout, nested retries,
and swallowed exceptions without modifying any file inside `vendor/Agent-S`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any, Final

import httpx
import openai
from gui_agents.s3.core.engine import LMMEngineOpenAI
from gui_agents.s3.core.mllm import LMMAgent
from gui_agents.s3.memory.procedural_memory import PROCEDURAL_MEMORY

from robbie.agents.conversation import (
    OPENAI_COMPATIBLE_CHAT_PROVIDERS,
    ProviderRequestError,
    ProviderTimeoutError,
    _extract_safe_provider_error_detail,
    _sanitize_endpoint_for_display,
)
from robbie.core.security import SecretScrubber, get_secret_scrubber

logger = logging.getLogger("robbie.agents.llm_engine")

DEFAULT_DESKTOP_HTTP_TIMEOUT_SECONDS: Final[float] = 45.0
DEFAULT_DESKTOP_MAX_RETRIES: Final[int] = 1
DEFAULT_DESKTOP_MAX_TOKENS: Final[int] = 4096

_ENV_API_KEY_BY_PROVIDER: Final[dict[str, str]] = {
    "openai": "OPENAI_API_KEY",
    "open_router": "OPENROUTER_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "qwen": "QWEN_API_KEY",
    "vllm": "vLLM_API_KEY",
    "parasail": "PARASAIL_API_KEY",
}

_ENV_BASE_URL_BY_PROVIDER: Final[dict[str, str]] = {
    "openai": "OPENAI_BASE_URL",
    "open_router": "OPEN_ROUTER_ENDPOINT_URL",
    "gemini": "GEMINI_ENDPOINT_URL",
    "deepseek": "DEEPSEEK_ENDPOINT_URL",
    "qwen": "QWEN_ENDPOINT_URL",
    "vllm": "vLLM_ENDPOINT_URL",
    "ollama": "OLLAMA_HOST",
    "parasail": "PARASAIL_ENDPOINT_URL",
}

_DEFAULT_BASE_URL_BY_PROVIDER: Final[dict[str, str]] = {
    "open_router": "https://openrouter.ai/api/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "deepseek": "https://api.deepseek.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "parasail": "https://api.parasail.io/v1",
}


def resolve_desktop_http_timeout() -> float:
    raw = os.environ.get("ROBBIE_DESKTOP_LLM_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_DESKTOP_HTTP_TIMEOUT_SECONDS
    try:
        parsed = float(raw)
    except ValueError:
        return DEFAULT_DESKTOP_HTTP_TIMEOUT_SECONDS
    return max(5.0, min(parsed, 120.0))


def _resolve_engine_base_url(provider: str, explicit_base_url: str | None) -> str | None:
    candidate = (explicit_base_url or "").strip()
    if not candidate:
        env_var = _ENV_BASE_URL_BY_PROVIDER.get(provider)
        if env_var:
            candidate = os.environ.get(env_var, "").strip()
    if not candidate:
        candidate = _DEFAULT_BASE_URL_BY_PROVIDER.get(provider, "")
    if not candidate:
        return None
    if provider == "ollama" and not candidate.rstrip("/").endswith("/v1"):
        return candidate.rstrip("/") + "/v1"
    return candidate


def _resolve_engine_api_key(provider: str, explicit_api_key: str | None) -> str:
    cleaned = (explicit_api_key or "").strip()
    if cleaned:
        return cleaned
    env_var = _ENV_API_KEY_BY_PROVIDER.get(provider)
    if env_var:
        env_val = os.environ.get(env_var, "").strip()
        if env_val:
            return env_val
    if provider == "ollama":
        return "ollama"
    raise ProviderRequestError(
        f"API key is missing for desktop provider '{provider}'.",
        status_code=401,
        retryable=False,
    )


class RobbieOpenAICompatibleEngine(LMMEngineOpenAI):
    """Bounded OpenAI-compatible engine compatible with Agent-S LMMAgent.

    Subclasses ``LMMEngineOpenAI`` so ``LMMAgent.add_message`` natively treats it
    as an OpenAI-style multimodal engine without modifying ``vendor/Agent-S``,
    while completely bypassing upstream ``@backoff.on_exception`` and 600s timeouts.
    """

    def __init__(
        self,
        *,
        engine_type: str = "openai",
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        rate_limit: int = -1,
        temperature: float | None = None,
        organization: str | None = None,
        http_timeout_seconds: float | None = None,
        max_retries: int = DEFAULT_DESKTOP_MAX_RETRIES,
        max_tokens: int = DEFAULT_DESKTOP_MAX_TOKENS,
        scrubber: SecretScrubber | None = None,
        client_factory: Callable[..., Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            model=model,
            rate_limit=rate_limit,
            temperature=temperature,
            organization=organization,
            **kwargs,
        )
        self.engine_type = engine_type
        self.http_timeout_seconds = (
            float(http_timeout_seconds)
            if http_timeout_seconds is not None
            else resolve_desktop_http_timeout()
        )
        self.max_retries = max(0, min(int(max_retries), 2))
        self.max_tokens = max(64, int(max_tokens))
        self._scrubber = scrubber or get_secret_scrubber()
        self._client_factory = client_factory or openai.OpenAI
        if self.api_key:
            self._scrubber.register(str(self.api_key))

    def generate(
        self,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        max_new_tokens: int | None = None,
        **kwargs: Any,
    ) -> str:
        api_key = _resolve_engine_api_key(self.engine_type, self.api_key)
        self._scrubber.register(api_key)
        organization = self.organization or os.environ.get("OPENAI_ORG_ID") or None
        resolved_base_url = _resolve_engine_base_url(self.engine_type, self.base_url)
        endpoint_display = _sanitize_endpoint_for_display(resolved_base_url)

        timeout_cfg = httpx.Timeout(
            connect=min(10.0, self.http_timeout_seconds),
            read=self.http_timeout_seconds,
            write=min(30.0, self.http_timeout_seconds),
            pool=min(10.0, self.http_timeout_seconds),
        )

        client_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": timeout_cfg,
            "max_retries": self.max_retries,
        }
        if resolved_base_url:
            client_kwargs["base_url"] = resolved_base_url
        if organization:
            client_kwargs["organization"] = organization

        resolved_temp = self.temperature if self.temperature is not None else temperature
        create_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": int(max_new_tokens) if max_new_tokens else self.max_tokens,
        }
        if resolved_temp is not None:
            create_kwargs["temperature"] = resolved_temp
        for key, value in kwargs.items():
            if value is not None and key not in create_kwargs:
                create_kwargs[key] = value

        logger.debug(
            "Desktop LLM request: provider=%s model=%s endpoint=%s messages=%d "
            "timeout=%.1fs max_retries=%d temperature=%s",
            self.engine_type,
            self.model,
            endpoint_display,
            len(messages),
            self.http_timeout_seconds,
            self.max_retries,
            resolved_temp,
        )

        client = self._client_factory(**client_kwargs)
        try:
            completion = client.chat.completions.create(**create_kwargs)
        except (openai.APITimeoutError, httpx.TimeoutException, TimeoutError) as exc:
            raise ProviderTimeoutError(
                f"Desktop provider '{self.engine_type}' timed out after "
                f"{self.http_timeout_seconds:.0f}s (model='{self.model}', "
                f"endpoint='{endpoint_display}')."
            ) from exc
        except openai.AuthenticationError as exc:
            raise ProviderRequestError(
                f"Desktop provider '{self.engine_type}' authentication failed "
                f"(HTTP 401 at '{endpoint_display}'). Verify your configured API key.",
                status_code=401,
                retryable=False,
            ) from exc
        except openai.PermissionDeniedError as exc:
            raise ProviderRequestError(
                f"Desktop provider '{self.engine_type}' denied access "
                f"(HTTP 403 at '{endpoint_display}').",
                status_code=403,
                retryable=False,
            ) from exc
        except openai.NotFoundError as exc:
            detail = _extract_safe_provider_error_detail(exc, self._scrubber)
            suffix = f": {detail}" if detail else ""
            raise ProviderRequestError(
                f"Desktop model '{self.model}' or endpoint '{endpoint_display}' "
                f"was not found (HTTP 404){suffix}",
                status_code=404,
                retryable=False,
            ) from exc
        except openai.BadRequestError as exc:
            detail = _extract_safe_provider_error_detail(exc, self._scrubber)
            suffix = f": {detail}" if detail else ""
            raise ProviderRequestError(
                f"Desktop provider '{self.engine_type}' rejected the request for model "
                f"'{self.model}' (HTTP 400){suffix}",
                status_code=400,
                retryable=False,
            ) from exc
        except openai.RateLimitError as exc:
            raise ProviderRequestError(
                f"Desktop provider '{self.engine_type}' rate limit exceeded (HTTP 429).",
                status_code=429,
                retryable=True,
            ) from exc
        except openai.APIConnectionError as exc:
            raise ProviderRequestError(
                f"Could not connect to desktop provider endpoint '{endpoint_display}'.",
                status_code=502,
                retryable=True,
            ) from exc
        except openai.APIStatusError as exc:
            status_code = getattr(exc, "status_code", 502)
            detail = _extract_safe_provider_error_detail(exc, self._scrubber)
            suffix = f": {detail}" if detail else ""
            raise ProviderRequestError(
                f"Desktop provider '{self.engine_type}' returned HTTP {status_code}{suffix}",
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
                f"Desktop model '{self.model}' returned no completion choices.",
                status_code=502,
                retryable=False,
            )
        message_obj = getattr(choices[0], "message", None)
        content = getattr(message_obj, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise ProviderRequestError(
                f"Desktop model '{self.model}' returned an empty response.",
                status_code=502,
                retryable=False,
            )
        return content


def build_robbie_lmm_agent(
    engine_params: dict[str, Any],
    system_prompt: str | None = None,
    *,
    http_timeout_seconds: float | None = None,
    max_retries: int = DEFAULT_DESKTOP_MAX_RETRIES,
    max_tokens: int = DEFAULT_DESKTOP_MAX_TOKENS,
    scrubber: SecretScrubber | None = None,
    client_factory: Callable[..., Any] | None = None,
) -> LMMAgent:
    """Create an Agent-S LMMAgent backed by Robbie's bounded OpenAI engine when applicable."""
    engine_type = str(engine_params.get("engine_type", "")).strip()
    if engine_type in OPENAI_COMPATIBLE_CHAT_PROVIDERS:
        engine = RobbieOpenAICompatibleEngine(
            http_timeout_seconds=http_timeout_seconds,
            max_retries=max_retries,
            max_tokens=max_tokens,
            scrubber=scrubber,
            client_factory=client_factory,
            **engine_params,
        )
        return LMMAgent(engine=engine, system_prompt=system_prompt)
    return LMMAgent(engine_params=engine_params, system_prompt=system_prompt)


def call_robbie_llm(
    agent: Any,
    temperature: float | None = None,
    use_thinking: bool = False,
    **kwargs: Any,
) -> str:
    """Invoke an LMMAgent once without nested retries or swallowed provider exceptions."""
    response = agent.get_response(
        temperature=temperature,
        use_thinking=use_thinking,
        **kwargs,
    )
    if not isinstance(response, str) or not response.strip():
        raise ProviderRequestError(
            "Configured desktop model returned an empty response.",
            status_code=502,
            retryable=False,
        )
    return response


def call_robbie_llm_formatted(
    generator: Any,
    format_checkers: list[Callable[[str], tuple[bool, str]]],
    *,
    max_format_retries: int = 2,
    cancel_check: Callable[[], bool] | None = None,
    **kwargs: Any,
) -> str:
    """Invoke the worker LMMAgent and retry formatting errors without masking provider errors."""
    if kwargs.get("messages") is None:
        messages = generator.messages.copy()
    else:
        messages = kwargs.pop("messages")

    response = ""
    for attempt in range(max_format_retries + 1):
        if cancel_check and cancel_check():
            return response

        response = call_robbie_llm(generator, messages=messages, **kwargs)

        feedback_msgs: list[str] = []
        for checker in format_checkers:
            ok, feedback = checker(response)
            if not ok:
                feedback_msgs.append(feedback)

        if not feedback_msgs:
            return response

        logger.warning(
            "Worker response failed formatting check (attempt %d/%d): %s",
            attempt + 1,
            max_format_retries + 1,
            "; ".join(feedback_msgs),
        )
        if attempt >= max_format_retries:
            break

        messages.append(
            {
                "role": "assistant",
                "content": [{"type": "text", "text": response}],
            }
        )
        delimiter = "\n- "
        formatting_feedback = f"- {delimiter.join(feedback_msgs)}"
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": PROCEDURAL_MEMORY.FORMATTING_FEEDBACK_PROMPT.replace(
                            "FORMATTING_FEEDBACK",
                            formatting_feedback,
                        ),
                    }
                ],
            }
        )

    return response
