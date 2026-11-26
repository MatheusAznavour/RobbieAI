"""End-to-end mocked tests for TaskRunner, FastAPI task routes, approvals, and WS events."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from robbie.agents.adapter import StepPrediction
from robbie.agents.runner import TaskRunner
from robbie.core.config import BackendSettings
from robbie.main import create_app
from robbie.tools.actions import (
    ClickAction,
    DoneAction,
    OpenApplicationAction,
    RobbieAction,
    TypeTextAction,
)
from robbie.tools.windows import ActionExecutionResult

TEST_TOKEN = "milestone2-test-bearer-token-1234567890"
TRUSTED_ORIGIN = "robbie://desktop"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_TOKEN}"}
WS_HEADERS = {"Authorization": f"Bearer {TEST_TOKEN}", "Origin": TRUSTED_ORIGIN}


def _task_request_payload(
    instruction: str = "Open the Help menu tab",
    *,
    max_steps: int = 5,
    step_timeout_seconds: float = 5.0,
    approval_timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    return {
        "instruction": instruction,
        "config": {
            "worker": {
                "provider": "openai",
                "model": "gpt-test",
                "api_key": "sk-worker-super-secret-must-not-leak",
            },
            "grounding": {
                "provider": "huggingface",
                "model": "ui-tars-test",
                "base_url": "http://127.0.0.1:8080",
                "api_key": "hf-ground-super-secret-must-not-leak",
                "grounding_width": 1920,
                "grounding_height": 1080,
            },
            "max_steps": max_steps,
            "step_timeout_seconds": step_timeout_seconds,
            "approval_timeout_seconds": approval_timeout_seconds,
            "dry_run": False,
        },
    }


class ScriptedAdapter:
    def __init__(
        self,
        steps: list[RobbieAction | Exception],
        *,
        delay_seconds: float = 0.0,
        gate: threading.Event | None = None,
    ) -> None:
        self._steps = list(steps)
        self._delay = delay_seconds
        self._gate = gate
        self.calls = 0

    def reset(self) -> None:
        pass

    def predict_step(
        self,
        instruction: str,
        screenshot_png: bytes,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> StepPrediction:
        if self._gate is not None:
            self._gate.wait(timeout=5.0)
        if self._delay > 0:
            time.sleep(self._delay)
        if not self._steps:
            action: RobbieAction = DoneAction()
        else:
            nxt = self._steps.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            action = nxt
        self.calls += 1
        return StepPrediction(
            action=action,
            plan=f"Step {self.calls} plan",
            plan_code=f"agent.{action.kind}()",
            reflection=None,
            notes=(),
        )


class RecordingDispatcher:
    def __init__(self) -> None:
        self.executed: list[RobbieAction] = []

    def execute(
        self,
        action: RobbieAction,
        *,
        stop_event: threading.Event | None = None,
    ) -> ActionExecutionResult:
        self.executed.append(action)
        return ActionExecutionResult(kind=action.kind, status="ok", detail="mocked")


class FakeScreenCapture:
    def capture_png(self, max_dimension: int = 2400) -> bytes:
        return b"fake-png"


def _make_app_with_mocks(
    adapter: ScriptedAdapter,
    dispatcher: RecordingDispatcher,
):
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
    return app


def _poll_task_state(
    client: TestClient,
    task_id: str,
    target_states: set[str],
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/tasks/{task_id}", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        last = resp.json()
        if last["state"] in target_states:
            return last
        time.sleep(0.02)
    raise AssertionError(f"Task {task_id} did not reach {target_states}; last={last}")


def test_task_endpoints_require_authentication() -> None:
    app = _make_app_with_mocks(ScriptedAdapter([DoneAction()]), RecordingDispatcher())
    with TestClient(app) as client:
        assert client.post("/tasks", json=_task_request_payload()).status_code == 401
        assert client.get("/tasks").status_code == 401
        assert client.post("/emergency-stop").status_code == 401
        assert client.post("/emergency-stop/reset").status_code == 401


def test_task_execution_streams_ws_events_non_blocking_and_without_leaking_secrets() -> None:
    adapter = ScriptedAdapter(
        [
            ClickAction(x=100, y=100, element_description="Click the Help menu tab"),
            DoneAction(),
        ],
        delay_seconds=0.25,
    )
    dispatcher = RecordingDispatcher()
    app = _make_app_with_mocks(adapter, dispatcher)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/status", headers=WS_HEADERS) as ws:
            initial_status = ws.receive_json()
            assert initial_status["type"] == "status"

            create_resp = client.post(
                "/tasks",
                headers=AUTH_HEADERS,
                json=_task_request_payload("Open Help menu"),
            )
            assert create_resp.status_code == 202
            task_id = create_resp.json()["task_id"]

            # Prove FastAPI asyncio event loop stays responsive while predict_step sleeps
            t0 = time.monotonic()
            health_resp = client.get("/health", headers=AUTH_HEADERS)
            elapsed = time.monotonic() - t0
            assert health_resp.status_code == 200
            assert elapsed < 0.2

            received_events: list[dict[str, Any]] = []
            while True:
                event = ws.receive_json()
                received_events.append(event)
                if event.get("type") == "task_completed":
                    break

            final_task = _poll_task_state(client, task_id, {"completed"})
            assert final_task["state"] == "completed"
            assert final_task["current_step"] == 2
            assert len(dispatcher.executed) == 1

            event_types = [e["type"] for e in received_events]
            assert "task_started" in event_types
            assert "step_started" in event_types
            assert "step_planned" in event_types
            assert "action_executed" in event_types
            assert "task_completed" in event_types

            for evt in received_events:
                assert evt["task_id"] == task_id
                serialized = str(evt)
                assert "sk-worker-super-secret-must-not-leak" not in serialized
                assert "hf-ground-super-secret-must-not-leak" not in serialized
                assert TEST_TOKEN not in serialized


def test_sensitive_action_requires_bound_approval_approve_and_deny_paths() -> None:
    # 1. Approved non-allowlisted document open executes after bound approval
    approve_adapter = ScriptedAdapter(
        [
            OpenApplicationAction(target="Downloads\\invoice.docx", target_kind="file"),
            DoneAction(),
        ]
    )
    approve_dispatcher = RecordingDispatcher()
    app = _make_app_with_mocks(approve_adapter, approve_dispatcher)

    with TestClient(app) as client:
        resp = client.post(
            "/tasks",
            headers=AUTH_HEADERS,
            json=_task_request_payload("Inspect invoice document"),
        )
        task_id = resp.json()["task_id"]

        waiting = _poll_task_state(client, task_id, {"awaiting_approval"})
        approval = waiting["pending_approval"]
        assert approval is not None
        assert len(approval["action_digest"]) == 64
        assert len(approve_dispatcher.executed) == 0

        resolve_resp = client.post(
            f"/tasks/{task_id}/approvals/{approval['approval_id']}",
            headers=AUTH_HEADERS,
            json={
                "approved": True,
                "step_id": approval["step_id"],
                "action_digest": approval["action_digest"],
                "reason": "User confirmed",
            },
        )
        assert resolve_resp.status_code == 200

        completed = _poll_task_state(client, task_id, {"completed"})
        assert completed["state"] == "completed"
        assert len(approve_dispatcher.executed) == 1

    # 2. Denied credential action is NOT executed and redacts typed text by default
    deny_adapter = ScriptedAdapter(
        [
            TypeTextAction(
                text="my-raw-password-value",
                element_description="Password input field",
            ),
            DoneAction(),
        ]
    )
    deny_dispatcher = RecordingDispatcher()
    app2 = _make_app_with_mocks(deny_adapter, deny_dispatcher)

    with TestClient(app2) as client:
        resp = client.post(
            "/tasks",
            headers=AUTH_HEADERS,
            json=_task_request_payload("Enter password"),
        )
        task_id = resp.json()["task_id"]

        waiting = _poll_task_state(client, task_id, {"awaiting_approval"})
        approval = waiting["pending_approval"]
        assert approval is not None
        assert approval["category"] == "credential_access"
        assert approval["action"]["text"] == "[REDACTED]"
        assert approval["action"]["text_length"] == len("my-raw-password-value")

        client.post(
            f"/tasks/{task_id}/approvals/{approval['approval_id']}",
            headers=AUTH_HEADERS,
            json={
                "approved": False,
                "step_id": approval["step_id"],
                "action_digest": approval["action_digest"],
                "reason": "User denied",
            },
        )
        cancelled = _poll_task_state(client, task_id, {"cancelled"})
        assert cancelled["state"] == "cancelled"
        assert deny_dispatcher.executed == []


def test_pause_resume_cancel_and_emergency_stop() -> None:
    # 1. Pause and resume
    gate = threading.Event()
    pause_adapter = ScriptedAdapter(
        [
            ClickAction(x=10, y=10, element_description="Focus the Help menu tab"),
            DoneAction(),
        ],
        gate=gate,
    )
    pause_dispatcher = RecordingDispatcher()
    app = _make_app_with_mocks(pause_adapter, pause_dispatcher)

    with TestClient(app) as client:
        task_id = client.post(
            "/tasks",
            headers=AUTH_HEADERS,
            json=_task_request_payload("View Help menu"),
        ).json()["task_id"]

        assert client.post(f"/tasks/{task_id}/pause", headers=AUTH_HEADERS).status_code == 200
        gate.set()
        paused = _poll_task_state(client, task_id, {"paused"})
        assert paused["state"] == "paused"
        assert pause_dispatcher.executed == []

        assert client.post(f"/tasks/{task_id}/resume", headers=AUTH_HEADERS).status_code == 200
        completed = _poll_task_state(client, task_id, {"completed"})
        assert completed["state"] == "completed"
        assert len(pause_dispatcher.executed) == 1

    # 2. Cancel during awaiting approval
    cancel_adapter = ScriptedAdapter(
        [OpenApplicationAction(target="Downloads\\report.pdf", target_kind="file"), DoneAction()]
    )
    cancel_dispatcher = RecordingDispatcher()
    app2 = _make_app_with_mocks(cancel_adapter, cancel_dispatcher)

    with TestClient(app2) as client:
        task_id = client.post(
            "/tasks",
            headers=AUTH_HEADERS,
            json=_task_request_payload("View report"),
        ).json()["task_id"]
        _poll_task_state(client, task_id, {"awaiting_approval"})

        assert client.post(f"/tasks/{task_id}/cancel", headers=AUTH_HEADERS).status_code == 200
        cancelled = _poll_task_state(client, task_id, {"cancelled"})
        assert cancelled["state"] == "cancelled"
        assert cancel_dispatcher.executed == []

    # 3. Emergency stop halts waiting task immediately
    stop_adapter = ScriptedAdapter(
        [OpenApplicationAction(target="Downloads\\report.pdf", target_kind="file"), DoneAction()]
    )
    stop_dispatcher = RecordingDispatcher()
    app3 = _make_app_with_mocks(stop_adapter, stop_dispatcher)

    with TestClient(app3) as client:
        task_id = client.post(
            "/tasks",
            headers=AUTH_HEADERS,
            json=_task_request_payload("View report"),
        ).json()["task_id"]
        _poll_task_state(client, task_id, {"awaiting_approval"})

        stop_resp = client.post("/emergency-stop", headers=AUTH_HEADERS)
        assert stop_resp.status_code == 200
        assert task_id in stop_resp.json()["affected_task_ids"]

        stopped = _poll_task_state(client, task_id, {"emergency_stopped"})
        assert stopped["state"] == "emergency_stopped"
        assert stop_dispatcher.executed == []


def test_task_failure_and_step_timeout_handling() -> None:
    err_adapter = ScriptedAdapter([RuntimeError("Upstream VLM returned HTTP 503")])
    app = _make_app_with_mocks(err_adapter, RecordingDispatcher())
    with TestClient(app) as client:
        task_id = client.post(
            "/tasks",
            headers=AUTH_HEADERS,
            json=_task_request_payload("Trigger failure"),
        ).json()["task_id"]
        failed = _poll_task_state(client, task_id, {"failed"})
        assert failed["state"] == "failed"
        assert "HTTP 503" in (failed["last_error"] or "")

    slow_adapter = ScriptedAdapter([DoneAction()], delay_seconds=0.6)
    app2 = _make_app_with_mocks(slow_adapter, RecordingDispatcher())
    with TestClient(app2) as client:
        task_id = client.post(
            "/tasks",
            headers=AUTH_HEADERS,
            json=_task_request_payload("Slow step", step_timeout_seconds=0.1),
        ).json()["task_id"]
        timed_out = _poll_task_state(client, task_id, {"failed"})
        assert timed_out["state"] == "failed"
        assert "timed out" in (timed_out["last_error"] or "").lower()
