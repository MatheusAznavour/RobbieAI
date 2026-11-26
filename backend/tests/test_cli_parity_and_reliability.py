"""Regression tests for M3.2A Agent-S CLI parity and desktop execution reliability."""

from __future__ import annotations

import inspect
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from gui_agents.s3.agents.grounding import OSWorldACI
from gui_agents.s3.core.engine import LMMEngineHuggingFace, LMMEngineOpenAI
from pydantic import SecretStr

from robbie.agents.adapter import OSWORLD_SUDO_LINE, AgentS3Adapter
from robbie.agents.conversation import ProviderRequestError
from robbie.agents.llm_engine import RobbieOpenAICompatibleEngine
from robbie.agents.runner import TaskRunner
from robbie.agents.windows_aci import RobbieWindowsACI
from robbie.core.config import BackendSettings
from robbie.main import create_app
from robbie.memory.conversations import ConversationStore
from robbie.providers.config import (
    AgentRuntimeConfig,
    GroundingModelConfig,
    WorkerModelConfig,
)
from robbie.providers.settings import (
    DEFAULT_GROUNDING_MODEL,
    DEFAULT_GROUNDING_PROVIDER,
    ProviderSettingsManager,
)
from robbie.tools.actions import ClickAction, DoneAction, OpenApplicationAction
from robbie.tools.safety import ActionSafetyPolicy
from robbie.tools.windows import (
    ActionExecutionError,
    DisplayInfo,
    WindowsAppLauncher,
    resolve_windows_app_path,
)
from tests.test_task_runner_and_api import (
    AUTH_HEADERS,
    TEST_TOKEN,
    TRUSTED_ORIGIN,
    FakeScreenCapture,
    RecordingDispatcher,
    ScriptedAdapter,
    _poll_task_state,
)

UNCENSORED_URL = "https://api.uncensoredllm.ai/v1"
MINIMAX_MODEL = "minimax-m3.1-flash-uncensored"
FAKE_KEY = "sk-m32a-secret-key-must-never-leak-123456"


def _clear_robbie_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "HF_TOKEN",
        "HF_ENDPOINT_URL",
        "ROBBIE_WORKER_PROVIDER",
        "ROBBIE_WORKER_MODEL",
        "ROBBIE_WORKER_BASE_URL",
        "ROBBIE_WORKER_TEMPERATURE",
        "ROBBIE_CHAT_PROVIDER",
        "ROBBIE_CHAT_MODEL",
        "ROBBIE_CHAT_BASE_URL",
        "ROBBIE_GROUNDING_PROVIDER",
        "ROBBIE_GROUNDING_MODEL",
        "ROBBIE_GROUNDING_URL",
        "ROBBIE_GROUNDING_WIDTH",
        "ROBBIE_GROUNDING_HEIGHT",
        "ROBBIE_MAX_TRAJECTORY_LENGTH",
        "ROBBIE_ENABLE_REFLECTION",
        "ROBBIE_DESKTOP_LLM_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)


class _MockOpenAIClient:
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
        pass


def _make_factory(records: list[dict[str, Any]], behavior: Any):
    def _factory(**kwargs: Any) -> _MockOpenAIClient:
        return _MockOpenAIClient(init_kwargs=kwargs, records=records, behavior=behavior)

    return _factory


def _http_response(status_code: int, body: dict[str, Any]) -> httpx.Response:
    req = httpx.Request("POST", f"{UNCENSORED_URL}/chat/completions")
    return httpx.Response(status_code=status_code, json=body, request=req)


# ---------------------------------------------------------------------------
# Priority 2: Full Agent-S action documentation parity in Worker prompt
# ---------------------------------------------------------------------------


def test_worker_prompt_preserves_full_agent_s_action_documentation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    cfg = AgentRuntimeConfig(
        worker=WorkerModelConfig(
            provider="openai",
            model=MINIMAX_MODEL,
            base_url=UNCENSORED_URL,
            api_key=SecretStr(FAKE_KEY),
        ),
        grounding=GroundingModelConfig(
            provider="openai",
            model=MINIMAX_MODEL,
            base_url=UNCENSORED_URL,
            api_key=SecretStr(FAKE_KEY),
            grounding_width=1920,
            grounding_height=1080,
        ),
        enable_reflection=False,
    )
    adapter = AgentS3Adapter(cfg, display_info=DisplayInfo(1920, 1080, 0, 0, 1920, 1080, 1, True))
    worker_prompt = adapter.agent.executor.generator_agent.system_prompt

    worker_prompt_normalized = " ".join(worker_prompt.split())
    # Every allowed action's core Agent-S documentation phrases are preserved
    for method_name in (
        "click",
        "switch_applications",
        "open",
        "type",
        "save_to_knowledge",
        "drag_and_drop",
        "scroll",
        "hotkey",
        "hold_and_press",
        "wait",
        "done",
        "fail",
    ):
        orig_doc = " ".join((inspect.getdoc(getattr(OSWorldACI, method_name)) or "").split())
        robbie_doc = " ".join(
            (inspect.getdoc(getattr(RobbieWindowsACI, method_name)) or "").split()
        )
        assert robbie_doc == orig_doc, f"Docstring mismatch for {method_name}"
        assert orig_doc in worker_prompt_normalized

    assert "Args:" in worker_prompt
    assert "['ctrl', 'c']" in worker_prompt
    assert "overwrite:bool" in worker_prompt
    # Unsafe / disabled actions and OSWorld sudo remain completely absent
    assert "call_code_agent" not in worker_prompt
    assert "set_cell_values" not in worker_prompt
    assert OSWORLD_SUDO_LINE not in worker_prompt
    assert "osworld-public-evaluation" not in worker_prompt


# ---------------------------------------------------------------------------
# Priority 3 & 4: CLI-equivalent worker/grounding config, bounded calls, 401/404 fail-fast
# ---------------------------------------------------------------------------


def test_cli_equivalent_openai_worker_and_grounding_configuration_and_bounded_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_robbie_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    monkeypatch.setenv("ROBBIE_WORKER_PROVIDER", "openai")
    monkeypatch.setenv("ROBBIE_WORKER_MODEL", MINIMAX_MODEL)
    monkeypatch.setenv("ROBBIE_WORKER_BASE_URL", UNCENSORED_URL)
    monkeypatch.setenv("ROBBIE_GROUNDING_PROVIDER", "openai")
    monkeypatch.setenv("ROBBIE_GROUNDING_MODEL", MINIMAX_MODEL)
    monkeypatch.setenv("ROBBIE_GROUNDING_URL", UNCENSORED_URL)
    monkeypatch.setenv("ROBBIE_GROUNDING_WIDTH", "1920")
    monkeypatch.setenv("ROBBIE_GROUNDING_HEIGHT", "1080")
    monkeypatch.setenv("ROBBIE_MAX_TRAJECTORY_LENGTH", "6")
    # Note: ROBBIE_WORKER_TEMPERATURE intentionally omitted -> CLI default (None)

    mgr = ProviderSettingsManager(storage_path=tmp_path / "settings.json")
    runtime_cfg = mgr.build_desktop_runtime_config()

    assert runtime_cfg.worker.provider == "openai"
    assert runtime_cfg.worker.model == MINIMAX_MODEL
    assert runtime_cfg.worker.base_url == UNCENSORED_URL
    assert runtime_cfg.worker.temperature is None
    assert runtime_cfg.grounding.provider == "openai"
    assert runtime_cfg.grounding.model == MINIMAX_MODEL
    assert runtime_cfg.grounding.base_url == UNCENSORED_URL
    assert runtime_cfg.grounding.grounding_width == 1920
    assert runtime_cfg.grounding.grounding_height == 1080
    assert runtime_cfg.max_trajectory_length == 6
    assert runtime_cfg.worker.api_key is not None
    assert runtime_cfg.grounding.api_key is not None
    assert (
        runtime_cfg.worker.api_key.get_secret_value()
        == runtime_cfg.grounding.api_key.get_secret_value()
        == FAKE_KEY
    )

    records: list[dict[str, Any]] = []
    responses = iter(
        [
            '(Next Action)\nClick icon.\n```python\nagent.click("Firefox taskbar icon")\n```',
            "(960, 540)",
        ]
    )

    def _responder(**_kwargs: Any) -> Any:
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=next(responses)))]
        )

    monkeypatch.setattr("shutil.which", lambda _name: None)
    adapter = AgentS3Adapter(
        runtime_cfg,
        display_info=DisplayInfo(1920, 1080, 0, 0, 1920, 1080, 1, True),
        http_timeout_seconds=35.0,
        max_retries=1,
        client_factory=_make_factory(records, _responder),
    )

    # Both worker and grounding use Robbie's bounded OpenAI engine (never HuggingFace)
    worker_engine = adapter.agent.executor.generator_agent.engine
    grounding_engine = adapter.aci.grounding_model.engine
    assert isinstance(worker_engine, RobbieOpenAICompatibleEngine)
    assert isinstance(worker_engine, LMMEngineOpenAI)
    assert isinstance(grounding_engine, RobbieOpenAICompatibleEngine)
    assert not isinstance(grounding_engine, LMMEngineHuggingFace)
    assert adapter.agent.executor.temperature is None

    pred = adapter.predict_step("Click Firefox icon", b"fake-png-bytes")
    assert isinstance(pred.action, ClickAction)
    assert pred.action.x == 960
    assert pred.action.y == 540

    # Two bounded calls: 1 worker + 1 grounding
    assert len(records) == 2
    for rec in records:
        assert rec["init"]["base_url"] == UNCENSORED_URL
        assert rec["init"]["api_key"] == FAKE_KEY
        assert rec["init"]["max_retries"] == 1
        assert isinstance(rec["init"]["timeout"], httpx.Timeout)
        assert rec["init"]["timeout"].read == 35.0
        assert rec["create"]["model"] == MINIMAX_MODEL
        # CLI parity: temperature omitted when None
        assert "temperature" not in rec["create"]


@pytest.mark.parametrize("status_code", [401, 404])
def test_desktop_provider_401_and_404_fail_fast_and_persist_task_summary(
    status_code: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_robbie_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    monkeypatch.setenv("ROBBIE_WORKER_PROVIDER", "openai")
    monkeypatch.setenv("ROBBIE_WORKER_MODEL", MINIMAX_MODEL)
    monkeypatch.setenv("ROBBIE_WORKER_BASE_URL", UNCENSORED_URL)
    monkeypatch.setenv("ROBBIE_GROUNDING_PROVIDER", "openai")
    monkeypatch.setenv("ROBBIE_GROUNDING_MODEL", MINIMAX_MODEL)
    monkeypatch.setenv("ROBBIE_GROUNDING_URL", UNCENSORED_URL)

    records: list[dict[str, Any]] = []
    if status_code == 401:
        err: Exception = openai.AuthenticationError(
            message=f"Bad key {FAKE_KEY}",
            response=_http_response(401, {"error": {"message": f"Bad key {FAKE_KEY}"}}),
            body={"error": {"message": f"Bad key {FAKE_KEY}"}},
        )
    else:
        err = openai.NotFoundError(
            message="No such model: bad-model",
            response=_http_response(404, {"error": {"message": "No such model: bad-model"}}),
            body={"error": {"message": "No such model: bad-model"}},
        )

    settings = BackendSettings(
        host="127.0.0.1",
        port=0,
        api_token=SecretStr(TEST_TOKEN),
        allowed_origins=(TRUSTED_ORIGIN,),
    )
    store = ConversationStore(storage_path=tmp_path / "conversations.json")
    mgr = ProviderSettingsManager(storage_path=tmp_path / "settings.json")
    app = create_app(settings, conversation_store=store, settings_manager=mgr)

    monkeypatch.setattr("shutil.which", lambda _name: None)
    runner = TaskRunner(
        broadcaster=app.state.runtime.connections.broadcast_json,
        adapter_factory=lambda cfg: AgentS3Adapter(
            cfg,
            display_info=DisplayInfo(1920, 1080, 0, 0, 1920, 1080, 1, True),
            http_timeout_seconds=10.0,
            max_retries=1,
            client_factory=_make_factory(records, err),
        ),
        dispatcher_factory=lambda _cfg: RecordingDispatcher(),
        screen_capture_factory=lambda: FakeScreenCapture(),
        on_task_finished=app.state.runtime.orchestrator.handle_task_terminal_state,
    )
    app.state.runtime.task_runner = runner
    app.state.runtime.orchestrator._runner = runner

    with TestClient(app) as client:
        conv_id = client.post("/conversations", headers=AUTH_HEADERS, json={}).json()[
            "conversation_id"
        ]
        send_resp = client.post(
            f"/conversations/{conv_id}/messages",
            headers=AUTH_HEADERS,
            json={"content": "Open Firefox", "mode": "desktop"},
        )
        assert send_resp.status_code == 200
        task_id = send_resp.json()["task"]["task_id"]

        failed = _poll_task_state(client, task_id, {"failed"}, timeout_s=5.0)
        assert failed["state"] == "failed"
        assert str(status_code) in (failed["last_error"] or "")
        assert FAKE_KEY not in (failed["last_error"] or "")
        # Deterministic 401/404 failed on the very first call (zero retries, zero swallowing)
        assert len(records) == 1

        # Persisted terminal task_summary written to conversation
        deadline = time.monotonic() + 3.0
        summaries: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            detail = client.get(f"/conversations/{conv_id}", headers=AUTH_HEADERS).json()
            summaries = [m for m in detail["messages"] if m["kind"] == "task_summary"]
            if summaries:
                break
            time.sleep(0.02)
        assert len(summaries) == 1
        assert "failed" in summaries[0]["content"].lower()
        assert FAKE_KEY not in summaries[0]["content"]


def test_stale_grounding_defaults_in_settings_json_do_not_override_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_robbie_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    monkeypatch.setenv("ROBBIE_WORKER_PROVIDER", "openai")
    monkeypatch.setenv("ROBBIE_WORKER_MODEL", MINIMAX_MODEL)
    monkeypatch.setenv("ROBBIE_WORKER_BASE_URL", UNCENSORED_URL)
    monkeypatch.setenv("ROBBIE_GROUNDING_PROVIDER", "openai")
    monkeypatch.setenv("ROBBIE_GROUNDING_MODEL", MINIMAX_MODEL)
    monkeypatch.setenv("ROBBIE_GROUNDING_URL", UNCENSORED_URL)

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "grounding_provider": DEFAULT_GROUNDING_PROVIDER,
                "grounding_model": DEFAULT_GROUNDING_MODEL,
                "grounding_url": "",
                "max_steps": 18,
            }
        ),
        encoding="utf-8",
    )

    mgr = ProviderSettingsManager(storage_path=settings_path)
    sel = mgr.get_selection()
    assert sel.grounding_provider == "openai"
    assert sel.grounding_model == MINIMAX_MODEL
    assert sel.grounding_url == UNCENSORED_URL
    assert sel.max_steps == 18

    # Explicit user grounding selection persists across reload
    updated = sel.model_copy(
        update={
            "grounding_provider": "vllm",
            "grounding_model": "custom-grounder",
            "grounding_url": "http://127.0.0.1:8080/v1",
        }
    )
    mgr.update_selection(updated)
    reloaded = ProviderSettingsManager(storage_path=settings_path).get_selection()
    assert reloaded.grounding_provider == "vllm"
    assert reloaded.grounding_model == "custom-grounder"
    assert reloaded.grounding_url == "http://127.0.0.1:8080/v1"


# ---------------------------------------------------------------------------
# Priority 5: Windows App Paths resolution & mandatory approval
# ---------------------------------------------------------------------------


def test_windows_app_paths_resolution_uses_shell_false_and_blocks_lolbins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firefox_exe = r"C:\Program Files\Mozilla Firefox\firefox.exe"
    powershell_exe = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

    fake_registry = {
        "firefox.exe": [f'"{firefox_exe}"'],
        "evilalias.exe": [powershell_exe],
        "withargs.exe": [f'"{firefox_exe}" --private-window'],
        "uncapp.exe": [r"\\evil-server\share\app.exe"],
    }

    resolved = resolve_windows_app_path(
        "Firefox",
        registry_reader=lambda name: fake_registry.get(name.lower(), []),
        file_exists=lambda p: p == firefox_exe,
    )
    assert resolved == firefox_exe

    # Registry entries pointing to LOLBins, CLI arguments, or UNC paths are rejected
    assert (
        resolve_windows_app_path(
            "evilalias",
            registry_reader=lambda name: fake_registry.get(name.lower(), []),
            file_exists=lambda _p: True,
        )
        is None
    )
    assert (
        resolve_windows_app_path(
            "withargs",
            registry_reader=lambda name: fake_registry.get(name.lower(), []),
            file_exists=lambda _p: True,
        )
        is None
    )
    assert (
        resolve_windows_app_path(
            "uncapp",
            registry_reader=lambda name: fake_registry.get(name.lower(), []),
            file_exists=lambda _p: True,
        )
        is None
    )

    # Approval remains strictly mandatory for Firefox in ActionSafetyPolicy
    policy = ActionSafetyPolicy()
    decision = policy.evaluate(OpenApplicationAction(target="Firefox", target_kind="application"))
    assert decision.allowed is True
    assert decision.requires_approval is True
    assert decision.category == "non_allowlisted_open"

    # Once approved, WindowsAppLauncher invokes Popen([exe], shell=False) without args
    popen_calls: list[tuple[list[str], bool]] = []

    def _fake_popen(argv: list[str], *, shell: bool = True) -> None:
        popen_calls.append((argv, shell))

    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setattr("shutil.which", lambda _name: None)
    launcher = WindowsAppLauncher(
        app_path_resolver=lambda name: firefox_exe if name.lower() == "firefox" else None,
        popen_fn=_fake_popen,
    )
    msg = launcher.launch("Firefox", "application")
    assert "firefox.exe" in msg.lower()
    assert popen_calls == [([firefox_exe], False)]

    # Interpreters/LOLBins remain hard-blocked even in launcher
    with pytest.raises(ActionExecutionError, match="Blocked"):
        launcher.launch("powershell", "application")


# ---------------------------------------------------------------------------
# Priority 6: Bounded adapter init timeout & Emergency Stop interrupting settle
# ---------------------------------------------------------------------------


def test_adapter_init_timeout_marks_task_failed_and_persists_summary(
    tmp_path: Path,
) -> None:
    settings = BackendSettings(
        host="127.0.0.1",
        port=0,
        api_token=SecretStr(TEST_TOKEN),
        allowed_origins=(TRUSTED_ORIGIN,),
    )
    store = ConversationStore(storage_path=tmp_path / "conversations.json")
    app = create_app(settings, conversation_store=store)

    def _slow_factory(_cfg: Any) -> Any:
        time.sleep(0.4)
        return ScriptedAdapter([DoneAction()])

    runner = TaskRunner(
        broadcaster=app.state.runtime.connections.broadcast_json,
        adapter_factory=_slow_factory,
        dispatcher_factory=lambda _cfg: RecordingDispatcher(),
        screen_capture_factory=lambda: FakeScreenCapture(),
        on_task_finished=app.state.runtime.orchestrator.handle_task_terminal_state,
    )
    app.state.runtime.task_runner = runner
    app.state.runtime.orchestrator._runner = runner

    with TestClient(app) as client:
        conv_id = client.post("/conversations", headers=AUTH_HEADERS, json={}).json()[
            "conversation_id"
        ]
        payload = {
            "instruction": "Open Help menu",
            "conversation_id": conv_id,
            "config": {
                "worker": {"provider": "openai", "model": MINIMAX_MODEL},
                "grounding": {
                    "provider": "openai",
                    "model": MINIMAX_MODEL,
                    "base_url": UNCENSORED_URL,
                },
                "init_timeout_seconds": 0.05,
                "post_action_settle_seconds": 0.0,
            },
        }
        task_id = client.post("/tasks", headers=AUTH_HEADERS, json=payload).json()["task_id"]
        failed = _poll_task_state(client, task_id, {"failed"}, timeout_s=3.0)
        assert "initialization timed out" in (failed["last_error"] or "").lower()

        deadline = time.monotonic() + 3.0
        summaries: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            detail = client.get(f"/conversations/{conv_id}", headers=AUTH_HEADERS).json()
            summaries = [m for m in detail["messages"] if m["kind"] == "task_summary"]
            if summaries:
                break
            time.sleep(0.02)
        assert len(summaries) == 1
        assert "failed" in summaries[0]["content"].lower()


def test_emergency_stop_interrupts_post_action_settle_delay_immediately() -> None:
    executed_event = threading.Event()

    class _SignalingDispatcher(RecordingDispatcher):
        def execute(self, action: Any, *, stop_event: threading.Event | None = None) -> Any:
            res = super().execute(action, stop_event=stop_event)
            executed_event.set()
            return res

    adapter = ScriptedAdapter(
        [
            ClickAction(x=100, y=100, element_description="Click the Help menu tab"),
            DoneAction(),
        ]
    )
    dispatcher = _SignalingDispatcher()

    settings = BackendSettings(
        host="127.0.0.1",
        port=0,
        api_token=SecretStr(TEST_TOKEN),
        allowed_origins=(TRUSTED_ORIGIN,),
    )
    app = create_app(settings)
    app.state.runtime.task_runner = TaskRunner(
        broadcaster=app.state.runtime.connections.broadcast_json,
        adapter_factory=lambda _cfg: adapter,
        dispatcher_factory=lambda _cfg: dispatcher,
        screen_capture_factory=lambda: FakeScreenCapture(),
    )

    with TestClient(app) as client:
        payload = {
            "instruction": "Click the Help menu tab",
            "config": {
                "worker": {"provider": "openai", "model": MINIMAX_MODEL},
                "grounding": {
                    "provider": "openai",
                    "model": MINIMAX_MODEL,
                    "base_url": UNCENSORED_URL,
                },
                "post_action_settle_seconds": 3.0,
            },
        }
        task_id = client.post("/tasks", headers=AUTH_HEADERS, json=payload).json()["task_id"]
        assert executed_event.wait(timeout=3.0)

        t0 = time.monotonic()
        stop_resp = client.post("/emergency-stop", headers=AUTH_HEADERS)
        assert stop_resp.status_code == 200
        stopped = _poll_task_state(client, task_id, {"emergency_stopped"}, timeout_s=1.0)
        elapsed = time.monotonic() - t0
        assert stopped["state"] == "emergency_stopped"
        # Interrupted the 3.0s settle delay well below 1 second
        assert elapsed < 0.8
        # Second step (DoneAction) never executed
        assert adapter.calls == 1


def test_provider_request_error_preserves_sanitized_message_without_secret() -> None:
    err = ProviderRequestError("Auth failed", status_code=401, retryable=False)
    assert err.status_code == 401
    assert err.retryable is False
