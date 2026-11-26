"""Bounded local persistence for Robbie conversations (strictly separate from task events)."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from robbie.core.runtime import utc_now_iso
from robbie.core.security import SecretScrubber, get_secret_scrubber

DEFAULT_CONVERSATION_TITLE: Final[str] = "New chat"
MAX_CONVERSATIONS: Final[int] = 100
MAX_MESSAGES_PER_CONVERSATION: Final[int] = 200
MAX_TITLE_LENGTH: Final[int] = 64

MessageRole = Literal["user", "assistant"]
MessageKind = Literal["chat", "task_started", "task_summary", "error"]


def resolve_data_dir(explicit_dir: str | None = None) -> Path:
    """Resolve user-local data storage directory on Windows without repo pollution."""
    configured = (explicit_dir or os.environ.get("ROBBIE_DATA_DIR", "")).strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "Robbie"
    return Path.home() / ".robbie"


def generate_conversation_title(first_message: str, scrubber: SecretScrubber) -> str:
    """Deterministically derive a safe, concise conversation title from a user prompt."""
    scrubbed = scrubber.scrub_text(first_message)
    single_line = re.sub(r"\s+", " ", scrubbed).strip()
    if not single_line:
        return DEFAULT_CONVERSATION_TITLE
    if len(single_line) <= MAX_TITLE_LENGTH:
        return single_line
    truncated = single_line[: MAX_TITLE_LENGTH - 1].rstrip()
    if " " in truncated:
        truncated = truncated.rsplit(" ", 1)[0]
    return f"{truncated}…"


@dataclass
class ChatMessageRecord:
    message_id: str
    conversation_id: str
    role: MessageRole
    content: str
    kind: MessageKind = "chat"
    task_id: str | None = None
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "role": self.role,
            "content": self.content,
            "kind": self.kind,
            "task_id": self.task_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatMessageRecord:
        return cls(
            message_id=str(data["message_id"]),
            conversation_id=str(data["conversation_id"]),
            role="assistant" if data.get("role") == "assistant" else "user",
            content=str(data.get("content", "")),
            kind=data.get("kind", "chat"),
            task_id=data.get("task_id"),
            created_at=str(data.get("created_at", utc_now_iso())),
        )


@dataclass
class ConversationRecord:
    conversation_id: str
    title: str = DEFAULT_CONVERSATION_TITLE
    title_is_custom: bool = False
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    messages: list[ChatMessageRecord] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)

    def to_summary_dict(self) -> dict[str, Any]:
        last_preview = ""
        if self.messages:
            last_preview = self.messages[-1].content[:120]
        return {
            "conversation_id": self.conversation_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": len(self.messages),
            "task_count": len(self.task_ids),
            "last_message_preview": last_preview,
        }

    def to_detail_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [m.to_dict() for m in self.messages],
            "task_ids": list(self.task_ids),
        }

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "title": self.title,
            "title_is_custom": self.title_is_custom,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [m.to_dict() for m in self.messages],
            "task_ids": list(self.task_ids),
        }

    @classmethod
    def from_storage_dict(cls, data: dict[str, Any]) -> ConversationRecord:
        return cls(
            conversation_id=str(data["conversation_id"]),
            title=str(data.get("title", DEFAULT_CONVERSATION_TITLE)),
            title_is_custom=bool(data.get("title_is_custom", False)),
            created_at=str(data.get("created_at", utc_now_iso())),
            updated_at=str(data.get("updated_at", utc_now_iso())),
            messages=[ChatMessageRecord.from_dict(m) for m in data.get("messages", [])],
            task_ids=[str(tid) for tid in data.get("task_ids", [])],
        )


class ConversationStore:
    """Thread-safe, bounded, secret-scrubbed JSON store for Robbie conversations.

    Persists ONLY conversation metadata, scrubbed user/assistant messages, and
    associated task IDs. Never stores raw sensitive typed text, task event traces,
    provider API keys, or backend bearer tokens.
    """

    def __init__(
        self,
        *,
        storage_path: Path | None = None,
        scrubber: SecretScrubber | None = None,
        max_conversations: int = MAX_CONVERSATIONS,
        max_messages_per_conversation: int = MAX_MESSAGES_PER_CONVERSATION,
    ) -> None:
        self._storage_path = storage_path
        self._scrubber = scrubber or get_secret_scrubber()
        self._max_conversations = max(1, max_conversations)
        self._max_messages = max(2, max_messages_per_conversation)
        self._lock = threading.RLock()
        self._conversations: dict[str, ConversationRecord] = {}
        self._load()

    def _load(self) -> None:
        if self._storage_path is None or not self._storage_path.exists():
            return
        try:
            raw = json.loads(self._storage_path.read_text(encoding="utf-8"))
            items = raw.get("conversations", []) if isinstance(raw, dict) else []
            for item in items[: self._max_conversations]:
                if isinstance(item, dict) and "conversation_id" in item:
                    record = ConversationRecord.from_storage_dict(item)
                    self._conversations[record.conversation_id] = record
        except Exception:
            # Fail safely without corrupting runtime if storage file is malformed.
            self._conversations = {}

    def _save(self) -> None:
        if self._storage_path is None:
            return
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(
            self._conversations.values(),
            key=lambda c: c.updated_at,
            reverse=True,
        )
        payload = {
            "version": 1,
            "conversations": [c.to_storage_dict() for c in ordered],
        }
        tmp_path = self._storage_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_path.replace(self._storage_path)

    def _enforce_bounds(self) -> None:
        if len(self._conversations) <= self._max_conversations:
            return
        oldest_first = sorted(
            self._conversations.values(),
            key=lambda c: c.updated_at,
        )
        for conv in oldest_first:
            if len(self._conversations) <= self._max_conversations:
                break
            del self._conversations[conv.conversation_id]

    def create_conversation(self, title: str | None = None) -> ConversationRecord:
        with self._lock:
            now = utc_now_iso()
            cleaned_title = self._scrubber.scrub_text((title or "").strip())
            record = ConversationRecord(
                conversation_id=str(uuid.uuid4()),
                title=cleaned_title[:MAX_TITLE_LENGTH]
                if cleaned_title
                else DEFAULT_CONVERSATION_TITLE,
                title_is_custom=bool(cleaned_title),
                created_at=now,
                updated_at=now,
            )
            self._conversations[record.conversation_id] = record
            self._enforce_bounds()
            self._save()
            return record

    def list_conversations(self, *, query: str = "") -> list[dict[str, Any]]:
        with self._lock:
            q = query.strip().lower()
            ordered = sorted(
                self._conversations.values(),
                key=lambda c: c.updated_at,
                reverse=True,
            )
            results: list[dict[str, Any]] = []
            for conv in ordered:
                if q:
                    in_title = q in conv.title.lower()
                    in_messages = any(q in m.content.lower() for m in conv.messages)
                    if not (in_title or in_messages):
                        continue
                results.append(conv.to_summary_dict())
            return results

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        with self._lock:
            return self._conversations.get(conversation_id)

    def rename_conversation(self, conversation_id: str, new_title: str) -> ConversationRecord:
        with self._lock:
            record = self._conversations.get(conversation_id)
            if record is None:
                raise KeyError(f"Conversation '{conversation_id}' not found.")
            cleaned = self._scrubber.scrub_text(new_title.strip())
            if not cleaned:
                raise ValueError("Conversation title must not be empty.")
            record.title = cleaned[:MAX_TITLE_LENGTH]
            record.title_is_custom = True
            record.updated_at = utc_now_iso()
            self._save()
            return record

    def delete_conversation(self, conversation_id: str) -> None:
        with self._lock:
            if conversation_id not in self._conversations:
                raise KeyError(f"Conversation '{conversation_id}' not found.")
            del self._conversations[conversation_id]
            self._save()

    def append_message(
        self,
        conversation_id: str,
        *,
        role: MessageRole,
        content: str,
        kind: MessageKind = "chat",
        task_id: str | None = None,
    ) -> ChatMessageRecord:
        with self._lock:
            record = self._conversations.get(conversation_id)
            if record is None:
                raise KeyError(f"Conversation '{conversation_id}' not found.")

            safe_content = self._scrubber.scrub_text(content)
            message = ChatMessageRecord(
                message_id=str(uuid.uuid4()),
                conversation_id=conversation_id,
                role=role,
                content=safe_content,
                kind=kind,
                task_id=task_id,
                created_at=utc_now_iso(),
            )
            record.messages.append(message)
            if len(record.messages) > self._max_messages:
                record.messages = record.messages[-self._max_messages :]

            if role == "user" and not record.title_is_custom and len(record.messages) == 1:
                record.title = generate_conversation_title(safe_content, self._scrubber)

            if task_id and task_id not in record.task_ids:
                record.task_ids.append(task_id)

            record.updated_at = utc_now_iso()
            self._save()
            return message

    def link_task(self, conversation_id: str, task_id: str) -> None:
        with self._lock:
            record = self._conversations.get(conversation_id)
            if record is None:
                return
            if task_id not in record.task_ids:
                record.task_ids.append(task_id)
                record.updated_at = utc_now_iso()
                self._save()
