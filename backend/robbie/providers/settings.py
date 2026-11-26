"""Privileged backend provider/model settings manager (never exposes API keys).

Configuration precedence (highest to lowest):
1. Explicit non-empty user selection persisted in ``settings.json`` (except when a
   legacy persisted entry contains only the built-in OpenAI fallback model paired with
   an empty/null base URL while the environment explicitly configures a custom model
   and endpoint).
2. Environment configuration (``ROBBIE_CHAT_*`` / ``ROBBIE_WORKER_*``,
   ``OPENAI_BASE_URL``, and provider-specific endpoint/credential variables).
3. Built-in fallback defaults.

Empty or ``null`` values in ``settings.json`` never erase valid environment defaults.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from robbie.providers.config import (
    AgentRuntimeConfig,
    GroundingModelConfig,
    SupportedProvider,
    WorkerModelConfig,
)

DEFAULT_OPENAI_MODEL: Final[str] = "gpt-5-2025-08-07"


class ProviderMetadata(BaseModel):
    """Public capability and credential-presence status for a supported provider."""

    model_config = ConfigDict(frozen=True)

    id: SupportedProvider
    label: str
    credential_env_var: str | None
    endpoint_env_var: str | None
    configured: bool
    supports_chat: bool = True
    supports_grounding: bool = True
    suggested_models: tuple[str, ...] = ()


PROVIDER_CATALOG: Final[tuple[dict[str, Any], ...]] = (
    {
        "id": "openai",
        "label": "OpenAI",
        "credential_env_var": "OPENAI_API_KEY",
        "endpoint_env_var": "OPENAI_BASE_URL",
        "suggested_models": (DEFAULT_OPENAI_MODEL, "gpt-4.1", "gpt-4o"),
    },
    {
        "id": "anthropic",
        "label": "Anthropic",
        "credential_env_var": "ANTHROPIC_API_KEY",
        "endpoint_env_var": None,
        "suggested_models": ("claude-sonnet-4-5-20250929", "claude-opus-4-5-20251101"),
    },
    {
        "id": "gemini",
        "label": "Google Gemini",
        "credential_env_var": "GEMINI_API_KEY",
        "endpoint_env_var": "GEMINI_ENDPOINT_URL",
        "suggested_models": ("gemini-2.5-pro", "gemini-2.5-flash"),
    },
    {
        "id": "open_router",
        "label": "OpenRouter",
        "credential_env_var": "OPENROUTER_API_KEY",
        "endpoint_env_var": "OPEN_ROUTER_ENDPOINT_URL",
        "suggested_models": ("openai/gpt-4o", "anthropic/claude-sonnet-4.5"),
    },
    {
        "id": "azure",
        "label": "Azure OpenAI",
        "credential_env_var": "AZURE_OPENAI_API_KEY",
        "endpoint_env_var": "AZURE_OPENAI_ENDPOINT",
        "suggested_models": ("gpt-4o",),
    },
    {
        "id": "ollama",
        "label": "Ollama (Local)",
        "credential_env_var": None,
        "endpoint_env_var": "OLLAMA_HOST",
        "suggested_models": ("qwen2.5vl:7b", "llama3.2"),
    },
    {
        "id": "vllm",
        "label": "vLLM (Local / Custom)",
        "credential_env_var": "vLLM_API_KEY",
        "endpoint_env_var": "vLLM_ENDPOINT_URL",
        "suggested_models": ("ui-tars-1.5-7b",),
    },
    {
        "id": "huggingface",
        "label": "Hugging Face Inference",
        "credential_env_var": "HF_TOKEN",
        "endpoint_env_var": "HF_ENDPOINT_URL",
        "suggested_models": ("ui-tars-1.5-7b", "ui-tars-72b"),
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "credential_env_var": "DEEPSEEK_API_KEY",
        "endpoint_env_var": "DEEPSEEK_ENDPOINT_URL",
        "suggested_models": ("deepseek-chat",),
    },
    {
        "id": "qwen",
        "label": "Qwen (DashScope)",
        "credential_env_var": "QWEN_API_KEY",
        "endpoint_env_var": "QWEN_ENDPOINT_URL",
        "suggested_models": ("qwen-vl-max",),
    },
    {
        "id": "parasail",
        "label": "Parasail",
        "credential_env_var": "PARASAIL_API_KEY",
        "endpoint_env_var": "PARASAIL_ENDPOINT_URL",
        "suggested_models": ("ui-tars-1.5-7b",),
    },
)


class ModelSelectionState(BaseModel):
    """Non-secret persisted model and execution preferences.

    Safety policy, approval gates, and LocalEnv disablement are non-configurable
    invariants enforced by Robbie's backend and never appear here.
    """

    model_config = ConfigDict(extra="forbid")

    chat_provider: SupportedProvider = "openai"
    chat_model: str = Field(default=DEFAULT_OPENAI_MODEL, min_length=1, max_length=200)
    chat_base_url: str | None = Field(default=None, max_length=500)

    worker_provider: SupportedProvider = "openai"
    worker_model: str = Field(default=DEFAULT_OPENAI_MODEL, min_length=1, max_length=200)
    worker_base_url: str | None = Field(default=None, max_length=500)

    grounding_provider: SupportedProvider = "huggingface"
    grounding_model: str = Field(default="ui-tars-1.5-7b", min_length=1, max_length=200)
    grounding_url: str = Field(default="", max_length=500)
    grounding_width: int = Field(default=1920, gt=0, le=16384)
    grounding_height: int = Field(default=1080, gt=0, le=16384)

    max_steps: int = Field(default=15, ge=1, le=100)
    enable_reflection: bool = True


def _provider_env_info(provider_id: str) -> dict[str, Any]:
    for entry in PROVIDER_CATALOG:
        if entry["id"] == provider_id:
            return entry
    raise ValueError(f"Unsupported provider: {provider_id}")


def _is_provider_configured(entry: dict[str, Any]) -> bool:
    cred_var = entry["credential_env_var"]
    endpoint_var = entry["endpoint_env_var"]
    if entry["id"] == "ollama":
        return bool(os.environ.get("OLLAMA_HOST", "").strip())
    if cred_var and not os.environ.get(cred_var, "").strip():
        return False
    if entry["id"] == "azure" and endpoint_var and not os.environ.get(endpoint_var, "").strip():
        return False
    return True


def _resolve_provider_secret(provider_id: str) -> SecretStr | None:
    entry = _provider_env_info(provider_id)
    cred_var = entry["credential_env_var"]
    if not cred_var:
        return None
    raw = os.environ.get(cred_var, "").strip()
    return SecretStr(raw) if raw else None


def _resolve_worker_temperature() -> float | None:
    raw = os.environ.get("ROBBIE_WORKER_TEMPERATURE", "").strip()
    if not raw:
        return None
    try:
        parsed = float(raw)
    except ValueError:
        return None
    if 0.0 <= parsed <= 2.0:
        return parsed
    return None


def _resolve_provider_base_url(
    provider_id: str,
    configured_url: str | None,
    *,
    allow_worker_fallback: bool = False,
) -> str | None:
    """Resolve provider base URL following explicit -> provider env -> worker env -> default."""
    if configured_url and configured_url.strip():
        return configured_url.strip()
    entry = _provider_env_info(provider_id)
    endpoint_var = entry["endpoint_env_var"]
    if endpoint_var:
        raw = os.environ.get(endpoint_var, "").strip()
        if raw:
            return raw
    if allow_worker_fallback:
        worker_url = os.environ.get("ROBBIE_WORKER_BASE_URL", "").strip()
        if worker_url:
            return worker_url
    if provider_id == "gemini":
        return "https://generativelanguage.googleapis.com/v1beta/openai/"
    if provider_id == "open_router":
        return "https://openrouter.ai/api/v1"
    return None


def _is_empty_persisted_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


class ProviderSettingsManager:
    """Manages non-secret model selections while keeping provider credentials in backend env."""

    def __init__(self, *, storage_path: Path | None = None) -> None:
        self._storage_path = storage_path
        self._lock = threading.RLock()
        self._selection = self._load_initial_selection()

    def _load_initial_selection(self) -> ModelSelectionState:
        """Build initial model selection respecting documented env + settings.json precedence."""
        env_worker_provider = os.environ.get("ROBBIE_WORKER_PROVIDER", "openai").strip() or "openai"
        env_worker_model_raw = os.environ.get("ROBBIE_WORKER_MODEL", "").strip()
        env_worker_model = env_worker_model_raw or DEFAULT_OPENAI_MODEL
        env_worker_url = (
            os.environ.get("ROBBIE_WORKER_BASE_URL", "").strip()
            or (
                os.environ.get("OPENAI_BASE_URL", "").strip()
                if env_worker_provider == "openai"
                else ""
            )
            or None
        )

        env_chat_provider = (
            os.environ.get("ROBBIE_CHAT_PROVIDER", "").strip() or env_worker_provider
        )
        env_chat_model_raw = os.environ.get("ROBBIE_CHAT_MODEL", "").strip()
        env_chat_model = env_chat_model_raw or env_worker_model
        env_chat_url = (
            os.environ.get("ROBBIE_CHAT_BASE_URL", "").strip()
            or (
                os.environ.get("OPENAI_BASE_URL", "").strip()
                if env_chat_provider == "openai"
                else ""
            )
            or (env_worker_url if env_chat_provider == env_worker_provider else None)
            or None
        )

        env_ground_provider = (
            os.environ.get("ROBBIE_GROUNDING_PROVIDER", "huggingface").strip() or "huggingface"
        )
        env_ground_model = (
            os.environ.get("ROBBIE_GROUNDING_MODEL", "ui-tars-1.5-7b").strip() or "ui-tars-1.5-7b"
        )
        env_ground_url = (
            os.environ.get("ROBBIE_GROUNDING_URL", "").strip()
            or os.environ.get("HF_ENDPOINT_URL", "").strip()
        )
        env_gw = int(os.environ.get("ROBBIE_GROUNDING_WIDTH", "1920") or "1920")
        env_gh = int(os.environ.get("ROBBIE_GROUNDING_HEIGHT", "1080") or "1080")
        env_reflection = os.environ.get("ROBBIE_ENABLE_REFLECTION", "true").strip().lower() in {
            "1",
            "true",
            "yes",
        }

        defaults: dict[str, Any] = {
            "chat_provider": env_chat_provider,
            "chat_model": env_chat_model,
            "chat_base_url": env_chat_url,
            "worker_provider": env_worker_provider,
            "worker_model": env_worker_model,
            "worker_base_url": env_worker_url,
            "grounding_provider": env_ground_provider,
            "grounding_model": env_ground_model,
            "grounding_url": env_ground_url,
            "grounding_width": env_gw,
            "grounding_height": env_gh,
            "enable_reflection": env_reflection,
        }

        if self._storage_path and self._storage_path.exists():
            try:
                saved = json.loads(self._storage_path.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    has_explicit_env_chat_model = bool(env_chat_model_raw or env_worker_model_raw)
                    has_explicit_env_worker_model = bool(env_worker_model_raw)

                    for key, value in saved.items():
                        # Never let empty/null persisted values overwrite valid env defaults.
                        if _is_empty_persisted_value(value):
                            continue

                        # Do not let a stale built-in fallback model paired with a null/empty
                        # base URL silently override an explicitly configured custom env model
                        # and endpoint.
                        if (
                            key == "chat_model"
                            and value == DEFAULT_OPENAI_MODEL
                            and _is_empty_persisted_value(saved.get("chat_base_url"))
                            and has_explicit_env_chat_model
                            and env_chat_model != DEFAULT_OPENAI_MODEL
                            and env_chat_url is not None
                        ):
                            continue
                        if (
                            key == "worker_model"
                            and value == DEFAULT_OPENAI_MODEL
                            and _is_empty_persisted_value(saved.get("worker_base_url"))
                            and has_explicit_env_worker_model
                            and env_worker_model != DEFAULT_OPENAI_MODEL
                            and env_worker_url is not None
                        ):
                            continue

                        defaults[key] = value
            except Exception:
                pass

        try:
            return ModelSelectionState(**defaults)
        except Exception:
            return ModelSelectionState()

    def _save(self) -> None:
        if self._storage_path is None:
            return
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._storage_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(self._selection.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self._storage_path)

    def get_selection(self) -> ModelSelectionState:
        with self._lock:
            return self._selection.model_copy()

    def update_selection(self, updated: ModelSelectionState) -> ModelSelectionState:
        with self._lock:
            self._selection = updated.model_copy()
            self._save()
            return self._selection.model_copy()

    def get_public_snapshot(self) -> dict[str, Any]:
        """Return safe settings metadata for UI rendering (zero secrets)."""
        with self._lock:
            selection = self._selection.model_copy()

        providers = [
            ProviderMetadata(
                id=entry["id"],
                label=entry["label"],
                credential_env_var=entry["credential_env_var"],
                endpoint_env_var=entry["endpoint_env_var"],
                configured=_is_provider_configured(entry),
                suggested_models=entry["suggested_models"],
            ).model_dump()
            for entry in PROVIDER_CATALOG
        ]

        chat_issues = self.validate_chat_readiness(selection)
        desktop_issues = self.validate_desktop_readiness(selection)

        return {
            "selection": selection.model_dump(),
            "providers": providers,
            "chat_ready": len(chat_issues) == 0,
            "chat_configuration_errors": chat_issues,
            "desktop_ready": len(desktop_issues) == 0,
            "desktop_configuration_errors": desktop_issues,
            "safety_policy_locked": True,
            "requires_restart_on_env_change": True,
        }

    def validate_chat_readiness(self, selection: ModelSelectionState | None = None) -> list[str]:
        sel = selection or self.get_selection()
        issues: list[str] = []
        entry = _provider_env_info(sel.chat_provider)
        if not _is_provider_configured(entry):
            env_hint = entry["credential_env_var"] or entry["endpoint_env_var"] or "provider env"
            issues.append(
                f"Chat provider '{entry['label']}' is not configured (set {env_hint} in backend "
                "environment)."
            )
        return issues

    def validate_desktop_readiness(self, selection: ModelSelectionState | None = None) -> list[str]:
        sel = selection or self.get_selection()
        issues: list[str] = []
        worker_entry = _provider_env_info(sel.worker_provider)
        if not _is_provider_configured(worker_entry):
            env_hint = worker_entry["credential_env_var"] or worker_entry["endpoint_env_var"]
            issues.append(f"Desktop worker provider '{worker_entry['label']}' requires {env_hint}.")

        ground_entry = _provider_env_info(sel.grounding_provider)
        if not _is_provider_configured(ground_entry):
            env_hint = ground_entry["credential_env_var"] or ground_entry["endpoint_env_var"]
            issues.append(
                f"Visual grounding provider '{ground_entry['label']}' requires {env_hint}."
            )

        resolved_ground_url = _resolve_provider_base_url(
            sel.grounding_provider,
            sel.grounding_url,
        )
        if not resolved_ground_url:
            issues.append(
                "Visual grounding endpoint URL is not configured "
                "(set Grounding URL in Settings or ROBBIE_GROUNDING_URL in .env)."
            )
        return issues

    def build_chat_model_config(self) -> WorkerModelConfig:
        sel = self.get_selection()
        env_worker_provider = os.environ.get("ROBBIE_WORKER_PROVIDER", "openai").strip() or "openai"
        return WorkerModelConfig(
            provider=sel.chat_provider,
            model=sel.chat_model,
            base_url=_resolve_provider_base_url(
                sel.chat_provider,
                sel.chat_base_url,
                allow_worker_fallback=(sel.chat_provider == env_worker_provider),
            ),
            api_key=_resolve_provider_secret(sel.chat_provider),
            temperature=_resolve_worker_temperature(),
        )

    def build_desktop_runtime_config(self) -> AgentRuntimeConfig:
        sel = self.get_selection()
        env_worker_provider = os.environ.get("ROBBIE_WORKER_PROVIDER", "openai").strip() or "openai"
        ground_url = _resolve_provider_base_url(sel.grounding_provider, sel.grounding_url)
        if not ground_url:
            raise ValueError(
                "Visual grounding endpoint URL is required before launching desktop tasks."
            )
        return AgentRuntimeConfig(
            worker=WorkerModelConfig(
                provider=sel.worker_provider,
                model=sel.worker_model,
                base_url=_resolve_provider_base_url(
                    sel.worker_provider,
                    sel.worker_base_url,
                    allow_worker_fallback=(sel.worker_provider == env_worker_provider),
                ),
                api_key=_resolve_provider_secret(sel.worker_provider),
                temperature=_resolve_worker_temperature(),
            ),
            grounding=GroundingModelConfig(
                provider=sel.grounding_provider,
                model=sel.grounding_model,
                base_url=ground_url,
                api_key=_resolve_provider_secret(sel.grounding_provider),
                grounding_width=sel.grounding_width,
                grounding_height=sel.grounding_height,
            ),
            max_steps=sel.max_steps,
            enable_reflection=sel.enable_reflection,
        )
