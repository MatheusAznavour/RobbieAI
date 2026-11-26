"""Tests for model configuration validation and isolated AgentS3Adapter execution."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from robbie.agents.adapter import OSWORLD_SUDO_LINE, AgentS3Adapter, SafeRobbieWorker
from robbie.providers.config import (
    AgentRuntimeConfig,
    GroundingModelConfig,
    WorkerModelConfig,
)
from robbie.tools.actions import ClickAction, SwitchApplicationAction, UnsafeActionCodeError
from robbie.tools.windows import DisplayInfo


def _make_runtime_config(
    *,
    worker_provider: str = "anthropic",
    grounding_provider: str = "vllm",
) -> AgentRuntimeConfig:
    return AgentRuntimeConfig(
        worker=WorkerModelConfig(
            provider=worker_provider,  # type: ignore[arg-type]
            model="claude-sonnet-test",
            api_key=SecretStr("sk-worker-secret-value-12345"),
            temperature=0.0,
        ),
        grounding=GroundingModelConfig(
            provider=grounding_provider,  # type: ignore[arg-type]
            model="ui-tars-1.5-7b",
            base_url="http://127.0.0.1:8080/v1",
            api_key=SecretStr("sk-ground-secret-value-67890"),
            grounding_width=1920,
            grounding_height=1080,
        ),
        max_steps=5,
        enable_reflection=False,
    )


def test_model_config_supports_multiple_providers_and_masks_secrets() -> None:
    cfg = _make_runtime_config(worker_provider="ollama", grounding_provider="open_router")
    assert "sk-worker-secret-value-12345" not in repr(cfg)
    assert "sk-ground-secret-value-67890" not in str(cfg)

    worker_params = cfg.worker.to_engine_params()
    assert worker_params["engine_type"] == "ollama"
    assert worker_params["model"] == "claude-sonnet-test"

    grounding_params = cfg.grounding.to_engine_params()
    assert grounding_params["engine_type"] == "open_router"
    assert grounding_params["grounding_width"] == 1920
    assert grounding_params["grounding_height"] == 1080


def test_model_config_rejects_invalid_provider_and_cannot_disable_safety() -> None:
    with pytest.raises(ValidationError):
        WorkerModelConfig(provider="unknown_provider", model="m")  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        GroundingModelConfig(
            provider="huggingface",
            model="ui-tars",
            base_url="http://127.0.0.1:8080",
            grounding_width=0,
            grounding_height=1080,
        )

    # Extra fields such as enable_local_env or disable_safety must be rejected
    with pytest.raises(ValidationError):
        AgentRuntimeConfig(
            worker=WorkerModelConfig(provider="openai", model="gpt-test"),
            grounding=GroundingModelConfig(
                provider="huggingface",
                model="ui-tars",
                base_url="http://127.0.0.1:8080",
            ),
            enable_local_env=True,  # type: ignore[call-arg]
        )


def test_agent_s3_adapter_initializes_safe_worker_and_strips_osworld_sudo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    cfg = _make_runtime_config()
    display = DisplayInfo(1920, 1080, 0, 0, 1920, 1080, 1, True)
    adapter = AgentS3Adapter(cfg, display_info=display)

    assert adapter.aci.env is None
    assert adapter.agent.platform == "windows"
    assert isinstance(adapter.agent.executor, SafeRobbieWorker)

    sys_prompt = adapter.agent.executor.generator_agent.system_prompt
    assert OSWORLD_SUDO_LINE not in sys_prompt
    assert "osworld-public-evaluation" not in sys_prompt
    assert "call_code_agent" not in sys_prompt
    assert "set_cell_values" not in sys_prompt


def test_agent_s3_adapter_predicts_structured_action_without_eval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    cfg = _make_runtime_config()
    display = DisplayInfo(1920, 1080, 0, 0, 1920, 1080, 1, True)
    adapter = AgentS3Adapter(cfg, display_info=display)

    # Mock generator LLM output and grounding model output
    monkeypatch.setattr(
        "robbie.agents.adapter.call_llm_formatted",
        lambda *_args, **_kwargs: (
            "(Next Action)\nClick the Save button.\n"
            "(Grounded Action)\n```python\n"
            'agent.click("The Save button in the toolbar", 1, "left")\n'
            "```"
        ),
    )
    monkeypatch.setattr("robbie.agents.windows_aci.call_llm_safe", lambda _m: "(400, 300)")

    prediction = adapter.predict_step("Save the current document", b"fake-png-bytes")
    assert isinstance(prediction.action, ClickAction)
    assert prediction.action.x == 400
    assert prediction.action.y == 300

    # Switch application produces structured Windows action, not `win+d` code string
    monkeypatch.setattr(
        "robbie.agents.adapter.call_llm_formatted",
        lambda *_args, **_kwargs: '```python\nagent.switch_applications("Notepad")\n```',
    )
    switch_pred = adapter.predict_step("Switch to Notepad", b"fake-png-bytes")
    assert isinstance(switch_pred.action, SwitchApplicationAction)
    assert switch_pred.action.app_name == "Notepad"


def test_safe_worker_rejects_code_injection_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    cfg = _make_runtime_config()
    display = DisplayInfo(1920, 1080, 0, 0, 1920, 1080, 1, True)
    adapter = AgentS3Adapter(cfg, display_info=display)

    monkeypatch.setattr(
        "robbie.agents.adapter.call_llm_formatted",
        lambda *_args, **_kwargs: "```python\n__import__('os').system('calc')\n```",
    )

    with pytest.raises(UnsafeActionCodeError):
        adapter.predict_step("Malicious test", b"fake-png-bytes")
