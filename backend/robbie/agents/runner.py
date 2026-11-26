"""Non-blocking Agent-S task orchestrator with persistent emergency stop,
interruptible dispatch, bound approvals, secret scrubbing, and bounded retention.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from robbie.core.runtime import utc_now_iso
from robbie.core.security import REDACTED_MARKER, SecretScrubber, get_secret_scrubber
from robbie.providers.config import AgentRuntimeConfig
from robbie.tools.actions import (
    DoneAction,
    FailAction,
    RobbieAction,
    SaveToKnowledgeAction,
    TypeTextAction,
    WaitAction,
)
from robbie.tools.safety import ActionSafetyPolicy, SafetyDecision
from robbie.tools.windows import (
    ActionExecutionResult,
    ActionInterruptedError,
    ScreenCaptureProtocol,
    WindowsActionDispatcher,
    WindowsScreenCapture,
)

if TYPE_CHECKING:
    from robbie.agents.adapter import StepPrediction

logger = logging.getLogger("robbie.agents.runner")

TaskState = Literal[
    "pending",
    "running",
    "paused",
    "awaiting_approval",
    "completed",
    "failed",
    "cancelled",
    "emergency_stopped",
]

TERMINAL_TASK_STATES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "emergency_stopped"}
)

DEFAULT_MAX_RETAINED_TASKS = 100


class EmergencyStopActiveError(RuntimeError):
    """Raised when a new task is requested while the emergency-stop latch is engaged."""


class ApprovalConflictError(RuntimeError):
    """Raised when attempting to resolve an already-resolved, expired, or cancelled approval."""


class ApprovalMismatchError(ValueError):
    """Raised when an approval confirmation does not match the pending step_id or action_digest."""


class StepPredictorProtocol(Protocol):
    def reset(self) -> None: ...
    def predict_step(
        self,
        instruction: str,
        screenshot_png: bytes,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> StepPrediction: ...


class ActionDispatcherProtocol(Protocol):
    def execute(
        self,
        action: RobbieAction,
        *,
        stop_event: threading.Event | None = None,
    ) -> ActionExecutionResult: ...


EventBroadcaster = Callable[[dict[str, Any]], Awaitable[None]]


def compute_action_digest(task_id: str, step_id: str, action: RobbieAction) -> str:
    """Compute a SHA-256 digest binding an approval to the exact task, step, and action."""
    canonical = json.dumps(
        {
            "task_id": task_id,
            "step_id": step_id,
            "action": action.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _sanitize_action_for_event(
    action: RobbieAction,
    category: str,
    scrubber: SecretScrubber | None = None,
) -> dict[str, Any]:
    """Serialize action metadata safely for events/approvals without ever exposing typed text."""
    active_scrubber = scrubber or get_secret_scrubber()
    payload = action.model_dump()
    if isinstance(action, TypeTextAction):
        raw_text = action.text
        payload["text"] = REDACTED_MARKER
        payload["text_length"] = len(raw_text)
        payload["has_non_ascii"] = any(ord(ch) > 127 for ch in raw_text)
        payload["action_category"] = category
    return active_scrubber.scrub_value(payload)  # type: ignore[no-any-return]


@dataclass
class PendingApproval:
    approval_id: str
    task_id: str
    step_id: str
    action_digest: str
    action: dict[str, Any]
    risk_level: str
    category: str
    reasons: list[str]
    created_at: str
    future: asyncio.Future[tuple[bool, str | None]] = field(repr=False)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "task_id": self.task_id,
            "step_id": self.step_id,
            "action_digest": self.action_digest,
            "action": self.action,
            "risk_level": self.risk_level,
            "category": self.category,
            "reasons": self.reasons,
            "created_at": self.created_at,
        }


@dataclass
class TaskRecord:
    task_id: str
    instruction: str
    conversation_id: str | None = None
    state: TaskState = "pending"
    current_step: int = 0
    max_steps: int = 15
    last_step_id: str | None = None
    last_error: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    pending_approval: PendingApproval | None = None
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    resolved_approval_ids: set[str] = field(default_factory=set, repr=False)
    resume_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    cancel_requested: bool = False
    emergency_stop_requested: bool = False
    bg_task: asyncio.Task[None] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.resume_event.set()

    def record_event(self, payload: dict[str, Any]) -> None:
        self.recent_events.append(payload)
        if len(self.recent_events) > 40:
            self.recent_events = self.recent_events[-40:]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "conversation_id": self.conversation_id,
            "instruction": self.instruction,
            "state": self.state,
            "current_step": self.current_step,
            "max_steps": self.max_steps,
            "last_step_id": self.last_step_id,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "pending_approval": (
                self.pending_approval.to_public_dict() if self.pending_approval else None
            ),
            "events": list(self.recent_events),
        }


def _default_adapter_factory(config: AgentRuntimeConfig) -> StepPredictorProtocol:
    """Lazily import Agent-S S3 off the FastAPI startup path."""
    from robbie.agents.adapter import AgentS3Adapter

    return AgentS3Adapter(config)


class TaskRunner:
    """Runs Agent-S S3 tasks asynchronously off FastAPI's event loop."""

    def __init__(
        self,
        *,
        broadcaster: EventBroadcaster,
        safety_policy: ActionSafetyPolicy | None = None,
        scrubber: SecretScrubber | None = None,
        adapter_factory: Callable[[AgentRuntimeConfig], StepPredictorProtocol] | None = None,
        dispatcher_factory: Callable[[AgentRuntimeConfig], ActionDispatcherProtocol] | None = None,
        screen_capture_factory: Callable[[], ScreenCaptureProtocol] | None = None,
        on_task_finished: Callable[[TaskRecord], Awaitable[None]] | None = None,
        max_retained_tasks: int = DEFAULT_MAX_RETAINED_TASKS,
    ) -> None:
        self._broadcaster = broadcaster
        self._safety_policy = safety_policy or ActionSafetyPolicy()
        self._scrubber = scrubber or get_secret_scrubber()
        self._adapter_factory = adapter_factory or _default_adapter_factory
        self._dispatcher_factory = dispatcher_factory or (
            lambda cfg: WindowsActionDispatcher(dry_run=cfg.dry_run)
        )
        self._screen_capture_factory = screen_capture_factory or (lambda: WindowsScreenCapture())
        self._on_task_finished = on_task_finished
        self._max_retained_tasks = max(1, max_retained_tasks)
        self._tasks: dict[str, TaskRecord] = {}
        self._emergency_stopped = False

    def set_on_task_finished(
        self,
        callback: Callable[[TaskRecord], Awaitable[None]] | None,
    ) -> None:
        self._on_task_finished = callback

    @property
    def is_emergency_stopped(self) -> bool:
        return self._emergency_stopped

    def get_task(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[dict[str, Any]]:
        return [record.to_public_dict() for record in self._tasks.values()]

    def _register_config_secrets(self, config: AgentRuntimeConfig) -> None:
        if config.worker.api_key is not None:
            self._scrubber.register(config.worker.api_key.get_secret_value())
        if config.grounding.api_key is not None:
            self._scrubber.register(config.grounding.api_key.get_secret_value())

    def _evict_completed_tasks(self) -> None:
        """Evict oldest terminal tasks without ever removing active or pending-approval tasks."""
        if len(self._tasks) <= self._max_retained_tasks:
            return
        for task_id in list(self._tasks.keys()):
            if len(self._tasks) <= self._max_retained_tasks:
                break
            candidate = self._tasks[task_id]
            if candidate.state in TERMINAL_TASK_STATES and candidate.pending_approval is None:
                del self._tasks[task_id]

    async def start_task(
        self,
        instruction: str,
        config: AgentRuntimeConfig,
        *,
        conversation_id: str | None = None,
    ) -> TaskRecord:
        # Persistent latch: never clear emergency stop automatically when starting a task.
        if self._emergency_stopped:
            raise EmergencyStopActiveError(
                "Emergency stop is active. Explicitly reset emergency stop before starting tasks."
            )

        self._register_config_secrets(config)
        self._evict_completed_tasks()

        task_id = str(uuid.uuid4())
        safe_instruction = self._scrubber.scrub_text(instruction)
        record = TaskRecord(
            task_id=task_id,
            conversation_id=conversation_id,
            instruction=safe_instruction,
            state="pending",
            max_steps=config.max_steps,
        )
        self._tasks[task_id] = record
        record.bg_task = asyncio.create_task(
            self._run_task(record, config, raw_instruction=instruction),
            name=f"robbie-task-{task_id[:8]}",
        )
        return record

    async def pause_task(self, task_id: str) -> TaskRecord:
        record = self._require_task(task_id)
        if record.state in TERMINAL_TASK_STATES or self._is_interrupted(record):
            raise ValueError(f"Cannot pause task in state '{record.state}'.")
        record.resume_event.clear()
        if record.state != "awaiting_approval":
            record.state = "paused"
            record.updated_at = utc_now_iso()
        await self._emit("task_paused", task_id=task_id, step_id=record.last_step_id)
        return record

    async def resume_task(self, task_id: str) -> TaskRecord:
        record = self._require_task(task_id)
        if record.state in TERMINAL_TASK_STATES or self._is_interrupted(record):
            raise ValueError(f"Cannot resume interrupted or terminal task '{record.state}'.")
        record.resume_event.set()
        if record.state == "paused":
            record.state = "running"
            record.updated_at = utc_now_iso()
        await self._emit("task_resumed", task_id=task_id, step_id=record.last_step_id)
        return record

    def _invalidate_pending_approval(self, record: TaskRecord, reason: str) -> None:
        pending = record.pending_approval
        if pending is None:
            return
        record.resolved_approval_ids.add(pending.approval_id)
        record.pending_approval = None
        if not pending.future.done():
            pending.future.set_result((False, reason))

    async def cancel_task(self, task_id: str) -> TaskRecord:
        record = self._require_task(task_id)
        if record.state in TERMINAL_TASK_STATES:
            return record
        record.cancel_requested = True
        record.stop_event.set()
        record.state = "cancelled"
        record.updated_at = utc_now_iso()
        record.resume_event.set()
        self._invalidate_pending_approval(record, "Task cancelled.")
        return record

    async def resolve_approval(
        self,
        task_id: str,
        approval_id: str,
        *,
        approved: bool,
        step_id: str,
        action_digest: str,
        reason: str | None = None,
    ) -> TaskRecord:
        record = self._require_task(task_id)

        if approval_id in record.resolved_approval_ids:
            raise ApprovalConflictError(
                f"Approval '{approval_id}' was already resolved, expired, or cancelled."
            )

        if record.state in TERMINAL_TASK_STATES or self._is_interrupted(record):
            raise ApprovalConflictError(
                f"Cannot resolve approval for task in state '{record.state}'."
            )

        pending = record.pending_approval
        if pending is None or pending.approval_id != approval_id:
            raise KeyError(f"No pending approval '{approval_id}' for task '{task_id}'.")

        if pending.future.done():
            record.resolved_approval_ids.add(approval_id)
            raise ApprovalConflictError(f"Approval '{approval_id}' is no longer active.")

        if pending.step_id != step_id or not secrets.compare_digest(
            pending.action_digest, action_digest
        ):
            raise ApprovalMismatchError(
                "Approval step_id or action_digest does not match the pending action."
            )

        record.resolved_approval_ids.add(approval_id)
        record.pending_approval = None
        pending.future.set_result((approved, reason))
        return record

    async def emergency_stop(self) -> list[str]:
        """Engage persistent emergency stop, abort in-flight actions, and invalidate approvals."""
        self._emergency_stopped = True
        affected: list[str] = []
        for record in self._tasks.values():
            if record.state not in TERMINAL_TASK_STATES or record.pending_approval is not None:
                record.emergency_stop_requested = True
                record.cancel_requested = True
                record.stop_event.set()
                record.state = "emergency_stopped"
                record.updated_at = utc_now_iso()
                record.resume_event.set()
                self._invalidate_pending_approval(record, "Emergency stop activated.")
                affected.append(record.task_id)

        await self._emit("emergency_stop", affected_task_ids=affected)
        return affected

    async def reset_emergency_stop(self) -> None:
        """Explicit operator reset required before accepting new tasks after emergency stop."""
        self._emergency_stopped = False
        await self._emit("emergency_stop_reset", emergency_stopped=False)

    async def shutdown(self) -> None:
        """Halt all running tasks cleanly during backend shutdown."""
        await self.emergency_stop()
        bg_tasks = [
            record.bg_task
            for record in self._tasks.values()
            if record.bg_task is not None and not record.bg_task.done()
        ]
        if bg_tasks:
            await asyncio.gather(*bg_tasks, return_exceptions=True)

    def _require_task(self, task_id: str) -> TaskRecord:
        record = self._tasks.get(task_id)
        if record is None:
            raise KeyError(f"Task '{task_id}' not found.")
        return record

    def _is_interrupted(self, record: TaskRecord) -> bool:
        return (
            record.cancel_requested
            or record.emergency_stop_requested
            or record.stop_event.is_set()
            or self._emergency_stopped
        )

    async def _finalize_interruption(self, record: TaskRecord, step_id: str | None) -> None:
        self._invalidate_pending_approval(record, "Task interrupted.")
        if record.emergency_stop_requested or self._emergency_stopped:
            record.state = "emergency_stopped"
            record.updated_at = utc_now_iso()
            await self._emit("task_emergency_stopped", task_id=record.task_id, step_id=step_id)
        else:
            record.state = "cancelled"
            record.updated_at = utc_now_iso()
            await self._emit("task_cancelled", task_id=record.task_id, step_id=step_id)

    async def _wait_if_paused(self, record: TaskRecord) -> None:
        if not record.resume_event.is_set() and not self._is_interrupted(record):
            record.state = "paused"
            record.updated_at = utc_now_iso()
            await record.resume_event.wait()
            if not self._is_interrupted(record):
                record.state = "running"
                record.updated_at = utc_now_iso()

    async def _run_task(
        self,
        record: TaskRecord,
        config: AgentRuntimeConfig,
        *,
        raw_instruction: str,
    ) -> None:
        step_id: str | None = None
        try:
            record.state = "running"
            record.updated_at = utc_now_iso()
            await self._emit(
                "task_started",
                task_id=record.task_id,
                max_steps=config.max_steps,
                dry_run=config.dry_run,
            )

            try:
                adapter = await asyncio.wait_for(
                    asyncio.to_thread(self._adapter_factory, config),
                    timeout=config.init_timeout_seconds,
                )
                dispatcher = await asyncio.wait_for(
                    asyncio.to_thread(self._dispatcher_factory, config),
                    timeout=config.init_timeout_seconds,
                )
                screen_capture = await asyncio.wait_for(
                    asyncio.to_thread(self._screen_capture_factory),
                    timeout=config.init_timeout_seconds,
                )
            except TimeoutError:
                record.state = "failed"
                record.last_error = (
                    f"Desktop agent initialization timed out after {config.init_timeout_seconds}s."
                )
                record.updated_at = utc_now_iso()
                await self._emit(
                    "task_failed",
                    task_id=record.task_id,
                    step_id=step_id,
                    error=record.last_error,
                )
                return

            if self._is_interrupted(record):
                await self._finalize_interruption(record, step_id)
                return

            for step_index in range(1, config.max_steps + 1):
                await self._wait_if_paused(record)
                if self._is_interrupted(record):
                    await self._finalize_interruption(record, step_id)
                    return

                step_id = f"{record.task_id}:step-{step_index}"
                record.current_step = step_index
                record.last_step_id = step_id
                record.updated_at = utc_now_iso()
                await self._emit(
                    "step_started",
                    task_id=record.task_id,
                    step_id=step_id,
                    step_index=step_index,
                )

                screenshot = await asyncio.wait_for(
                    asyncio.to_thread(screen_capture.capture_png),
                    timeout=config.step_timeout_seconds,
                )

                if self._is_interrupted(record):
                    await self._finalize_interruption(record, step_id)
                    return

                prediction = await asyncio.wait_for(
                    asyncio.to_thread(
                        adapter.predict_step,
                        raw_instruction,
                        screenshot,
                        cancel_check=lambda: self._is_interrupted(record),
                    ),
                    timeout=config.step_timeout_seconds,
                )

                await self._wait_if_paused(record)
                if self._is_interrupted(record):
                    await self._finalize_interruption(record, step_id)
                    return

                decision: SafetyDecision = self._safety_policy.evaluate(
                    prediction.action,
                    task_instruction=raw_instruction,
                )
                safe_action = _sanitize_action_for_event(
                    prediction.action,
                    decision.category,
                    self._scrubber,
                )

                await self._emit(
                    "step_planned",
                    task_id=record.task_id,
                    step_id=step_id,
                    action=safe_action,
                    risk_level=decision.risk_level,
                    category=decision.category,
                    requires_approval=decision.requires_approval,
                )

                if isinstance(prediction.action, DoneAction):
                    record.state = "completed"
                    record.updated_at = utc_now_iso()
                    await self._emit(
                        "task_completed",
                        task_id=record.task_id,
                        step_id=step_id,
                        steps_executed=step_index,
                    )
                    return

                if isinstance(prediction.action, FailAction):
                    record.state = "failed"
                    record.last_error = "Agent determined the task cannot be completed."
                    record.updated_at = utc_now_iso()
                    await self._emit(
                        "task_failed",
                        task_id=record.task_id,
                        step_id=step_id,
                        error=record.last_error,
                    )
                    return

                if not decision.allowed:
                    reason = self._scrubber.scrub_text(
                        "; ".join(decision.reasons) or "Action blocked by safety policy."
                    )
                    await self._emit(
                        "action_rejected",
                        task_id=record.task_id,
                        step_id=step_id,
                        action=safe_action,
                        category=decision.category,
                        reason=reason,
                    )
                    record.state = "failed"
                    record.last_error = reason
                    record.updated_at = utc_now_iso()
                    await self._emit(
                        "task_failed",
                        task_id=record.task_id,
                        step_id=step_id,
                        error=reason,
                    )
                    return

                if decision.requires_approval:
                    approved, approval_reason = await self._request_approval(
                        record=record,
                        step_id=step_id,
                        action=prediction.action,
                        safe_action=safe_action,
                        decision=decision,
                        timeout_seconds=config.approval_timeout_seconds,
                    )
                    if self._is_interrupted(record):
                        await self._finalize_interruption(record, step_id)
                        return
                    if not approved:
                        deny_reason = self._scrubber.scrub_text(
                            approval_reason or "Sensitive action was denied by user."
                        )
                        await self._emit(
                            "action_rejected",
                            task_id=record.task_id,
                            step_id=step_id,
                            action=safe_action,
                            category=decision.category,
                            reason=deny_reason,
                        )
                        record.state = "cancelled"
                        record.last_error = deny_reason
                        record.updated_at = utc_now_iso()
                        await self._emit(
                            "task_cancelled",
                            task_id=record.task_id,
                            step_id=step_id,
                            reason=deny_reason,
                        )
                        return

                await self._wait_if_paused(record)
                if self._is_interrupted(record):
                    await self._finalize_interruption(record, step_id)
                    return

                exec_result = await asyncio.wait_for(
                    asyncio.to_thread(
                        dispatcher.execute,
                        prediction.action,
                        stop_event=record.stop_event,
                    ),
                    timeout=config.step_timeout_seconds,
                )
                await self._emit(
                    "action_executed",
                    task_id=record.task_id,
                    step_id=step_id,
                    action=safe_action,
                    result_status=exec_result.status,
                    detail=self._scrubber.scrub_text(exec_result.detail),
                )

                # Interruptible post-action settle delay so Windows UI finishes rendering
                # before the next screenshot while preserving instant Emergency Stop response.
                if config.post_action_settle_seconds > 0 and not isinstance(
                    prediction.action,
                    (DoneAction, FailAction, SaveToKnowledgeAction, WaitAction),
                ):
                    interrupted = await asyncio.to_thread(
                        record.stop_event.wait,
                        config.post_action_settle_seconds,
                    )
                    if interrupted or self._is_interrupted(record):
                        await self._finalize_interruption(record, step_id)
                        return

            record.state = "failed"
            record.last_error = (
                f"Reached maximum step limit ({config.max_steps}) without completion."
            )
            record.updated_at = utc_now_iso()
            await self._emit(
                "task_failed",
                task_id=record.task_id,
                step_id=step_id,
                error=record.last_error,
            )
        except ActionInterruptedError:
            await self._finalize_interruption(record, step_id)
        except TimeoutError as exc:
            detail = self._scrubber.scrub_text(str(exc)) if str(exc) else ""
            record.state = "failed"
            record.last_error = detail or f"Task timed out after {config.step_timeout_seconds}s."
            record.updated_at = utc_now_iso()
            await self._emit(
                "task_failed",
                task_id=record.task_id,
                step_id=step_id,
                error=record.last_error,
            )
        except asyncio.CancelledError:
            record.cancel_requested = True
            record.stop_event.set()
            await self._finalize_interruption(record, step_id)
            raise
        except Exception as exc:
            safe_error = self._scrubber.scrub_text(str(exc) or type(exc).__name__)
            logger.error("Task %s failed: %s", record.task_id, safe_error)
            record.state = "failed"
            record.last_error = safe_error
            record.updated_at = utc_now_iso()
            await self._emit(
                "task_failed",
                task_id=record.task_id,
                step_id=step_id,
                error=safe_error,
            )
        finally:
            if record.state in TERMINAL_TASK_STATES and self._on_task_finished is not None:
                try:
                    await self._on_task_finished(record)
                except Exception:
                    logger.exception(
                        "Failed to run task-finished callback for task %s.",
                        record.task_id,
                    )
            self._evict_completed_tasks()

    async def _request_approval(
        self,
        *,
        record: TaskRecord,
        step_id: str,
        action: RobbieAction,
        safe_action: dict[str, Any],
        decision: SafetyDecision,
        timeout_seconds: float,
    ) -> tuple[bool, str | None]:
        loop = asyncio.get_running_loop()
        approval_id = str(uuid.uuid4())
        action_digest = compute_action_digest(record.task_id, step_id, action)
        future: asyncio.Future[tuple[bool, str | None]] = loop.create_future()
        pending = PendingApproval(
            approval_id=approval_id,
            task_id=record.task_id,
            step_id=step_id,
            action_digest=action_digest,
            action=safe_action,
            risk_level=decision.risk_level,
            category=decision.category,
            reasons=[self._scrubber.scrub_text(r) for r in decision.reasons],
            created_at=utc_now_iso(),
            future=future,
        )
        record.pending_approval = pending
        record.state = "awaiting_approval"
        record.updated_at = utc_now_iso()

        await self._emit(
            "approval_required",
            task_id=record.task_id,
            step_id=step_id,
            approval=pending.to_public_dict(),
        )

        try:
            approved, reason = await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError:
            record.resolved_approval_ids.add(approval_id)
            approved, reason = False, f"Approval timed out after {timeout_seconds}s."
        finally:
            record.resolved_approval_ids.add(approval_id)
            if record.pending_approval is pending:
                record.pending_approval = None
            if not self._is_interrupted(record) and record.state == "awaiting_approval":
                record.state = "running"
                record.updated_at = utc_now_iso()

        await self._emit(
            "approval_resolved",
            task_id=record.task_id,
            step_id=step_id,
            approval_id=approval_id,
            action_digest=action_digest,
            approved=approved,
            reason=self._scrubber.scrub_text(reason) if reason else None,
        )
        return approved, reason

    async def _emit(self, event_type: str, **fields: Any) -> None:
        task_id = fields.get("task_id")
        if isinstance(task_id, str) and "conversation_id" not in fields:
            task_rec = self._tasks.get(task_id)
            if task_rec is not None and task_rec.conversation_id is not None:
                fields["conversation_id"] = task_rec.conversation_id
        payload: dict[str, Any] = {
            "type": event_type,
            "timestamp": utc_now_iso(),
            **fields,
        }
        scrubbed_payload = self._scrubber.scrub_value(payload)
        if isinstance(task_id, str):
            task_rec = self._tasks.get(task_id)
            if task_rec is not None:
                task_rec.record_event(scrubbed_payload)
        await self._broadcaster(scrubbed_payload)
