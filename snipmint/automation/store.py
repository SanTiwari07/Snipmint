"""Persistent automation state: tasks, batches, n8n executions, the event outbox, idempotency.

Same pattern as the YouTube store: a small interface (``AutomationStore``) with a JSON-file
implementation written atomically, under ``<home>/integrations/automation/``. Everything about
scheduling lives here (not in n8n), so an n8n restart loses nothing.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from ..youtube.settings import integrations_dir
from ..youtube.store import Privacy, _atomic_write, _read

TaskStatus = Literal[
    "AWAITING_APPROVAL", "READY", "SCHEDULED", "PROCESSING", "UPLOADING",
    "PUBLISHED", "FAILED", "CANCELLED", "SKIPPED",
]  # fmt: skip
OPEN: tuple[str, ...] = ("AWAITING_APPROVAL", "READY", "SCHEDULED", "PROCESSING", "UPLOADING")
TERMINAL: tuple[str, ...] = ("PUBLISHED", "FAILED", "CANCELLED", "SKIPPED")

MAX_EXECUTIONS = 300
MAX_DELIVERED_EVENTS = 200
IDEMPOTENCY_TTL = 48 * 3600


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class AutomationSettings(BaseModel):
    """What the person controls in the UI (no secrets)."""

    enabled: bool = False
    auto_approve: bool = Field(False, description="Approve generated metadata automatically (off by default)")
    language: str = "en"
    privacy: Privacy = "private"
    made_for_kids: bool = False
    batch_delay_seconds: int = Field(30, ge=0, le=3600)
    batch_concurrency: int = Field(1, ge=1, le=4)
    max_attempts: int = Field(4, ge=1, le=10)
    max_lateness_minutes: int = Field(360, ge=5, le=10080, description="A scheduled task later than this is not published")


class ClipRef(BaseModel):
    job_id: str = Field(min_length=1, max_length=80)
    clip_file: str = Field(pattern=r"^clip_\d{2}\.mp4$")
    clip_id: str = Field(min_length=1, max_length=80)
    source_id: str = Field(min_length=1, max_length=80)


class HistoryEntry(BaseModel):
    at: float = Field(default_factory=time.time)
    status: str
    message: str = ""


class AutomationTask(BaseModel):
    id: str = Field(default_factory=lambda: new_id("task"))
    kind: Literal["publish", "scheduled", "batch"] = "publish"
    status: TaskStatus = "READY"
    clip: ClipRef
    title: str = ""
    privacy: Privacy = "private"
    made_for_kids: bool = False
    language: str = "en"
    channel_id: str | None = None
    run_at: str | None = None  # RFC 3339 UTC, for scheduled tasks
    batch_id: str | None = None
    publish_item_id: str | None = None
    attempts: int = 0
    max_attempts: int = 4
    next_attempt_at: float | None = None
    error: str | None = None
    error_kind: str | None = None
    video_url: str | None = None
    executions: list[str] = Field(default_factory=list)
    history: list[HistoryEntry] = Field(default_factory=list)
    created: float = Field(default_factory=time.time)
    updated: float = Field(default_factory=time.time)

    def log(self, status: str, message: str = "") -> None:
        self.history = [*self.history[-19:], HistoryEntry(status=status, message=message[:300])]


class Batch(BaseModel):
    id: str = Field(default_factory=lambda: new_id("batch"))
    task_ids: list[str]
    delay_seconds: int = 30
    concurrency: int = 1
    created: float = Field(default_factory=time.time)
    completed_at: float | None = None
    summary: dict[str, Any] | None = None


ExecutionStatus = Literal["running", "success", "error", "waiting", "cancelled"]


class Execution(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    workflow: str = Field("", max_length=120)
    status: ExecutionStatus = "running"
    task_id: str | None = None
    batch_id: str | None = None
    message: str = Field("", max_length=1000)
    started: float = Field(default_factory=time.time)
    updated: float = Field(default_factory=time.time)


class OutboxEvent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("evt"))
    type: str
    payload: dict[str, Any]
    status: Literal["pending", "delivered", "dead"] = "pending"
    attempts: int = 0
    next_attempt_at: float = Field(default_factory=time.time)
    last_error: str | None = None
    created: float = Field(default_factory=time.time)


class AutomationStore(Protocol):
    def get_settings(self) -> AutomationSettings: ...
    def save_settings(self, s: AutomationSettings) -> None: ...
    def list_tasks(self) -> list[AutomationTask]: ...
    def get_task(self, task_id: str) -> AutomationTask | None: ...
    def save_task(self, task: AutomationTask) -> None: ...
    def get_batch(self, batch_id: str) -> Batch | None: ...
    def save_batch(self, batch: Batch) -> None: ...
    def list_executions(self) -> list[Execution]: ...
    def save_execution(self, e: Execution) -> None: ...
    def list_events(self) -> list[OutboxEvent]: ...
    def save_event(self, e: OutboxEvent) -> None: ...
    def idempotent(self, key: str) -> dict | None: ...
    def remember(self, key: str, response: dict) -> None: ...


class JsonAutomationStore:
    """One JSON file, guarded by a lock, rewritten atomically on every change."""

    def __init__(self, folder: Path | None = None):
        self.folder = Path(folder) if folder else integrations_dir() / "automation"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "state.json"
        self.lock = threading.RLock()

    def _load(self) -> dict:
        return _read(self.path, {"settings": {}, "tasks": {}, "batches": {}, "executions": {},
                                 "events": {}, "idempotency": {}})  # fmt: skip

    def _save(self, data: dict) -> None:
        _atomic_write(self.path, data)

    def _put(self, section: str, key: str, value: dict) -> None:
        with self.lock:
            data = self._load()
            data.setdefault(section, {})[key] = value
            self._prune(data)
            self._save(data)

    @staticmethod
    def _prune(data: dict) -> None:
        ex = data.get("executions", {})
        if len(ex) > MAX_EXECUTIONS:
            for k in sorted(ex, key=lambda k: ex[k].get("updated", 0))[: len(ex) - MAX_EXECUTIONS]:
                del ex[k]
        ev = data.get("events", {})
        done = sorted((k for k, v in ev.items() if v.get("status") == "delivered"), key=lambda k: ev[k].get("created", 0))
        for k in done[: max(0, len(done) - MAX_DELIVERED_EVENTS)]:
            del ev[k]
        now = time.time()
        idem = data.get("idempotency", {})
        for k in [k for k, v in idem.items() if now - v.get("created", 0) > IDEMPOTENCY_TTL]:
            del idem[k]

    # settings
    def get_settings(self) -> AutomationSettings:
        with self.lock:
            return AutomationSettings.model_validate(self._load().get("settings") or {})

    def save_settings(self, s: AutomationSettings) -> None:
        with self.lock:
            data = self._load()
            data["settings"] = s.model_dump()
            self._save(data)

    # tasks
    def list_tasks(self) -> list[AutomationTask]:
        with self.lock:
            items = [AutomationTask.model_validate(v) for v in self._load().get("tasks", {}).values()]
        return sorted(items, key=lambda t: t.created, reverse=True)

    def get_task(self, task_id: str) -> AutomationTask | None:
        with self.lock:
            raw = self._load().get("tasks", {}).get(task_id)
        return AutomationTask.model_validate(raw) if raw else None

    def save_task(self, task: AutomationTask) -> None:
        task.updated = time.time()
        self._put("tasks", task.id, task.model_dump())

    # batches
    def get_batch(self, batch_id: str) -> Batch | None:
        with self.lock:
            raw = self._load().get("batches", {}).get(batch_id)
        return Batch.model_validate(raw) if raw else None

    def save_batch(self, batch: Batch) -> None:
        self._put("batches", batch.id, batch.model_dump())

    # executions
    def list_executions(self) -> list[Execution]:
        with self.lock:
            items = [Execution.model_validate(v) for v in self._load().get("executions", {}).values()]
        return sorted(items, key=lambda e: e.updated, reverse=True)

    def save_execution(self, e: Execution) -> None:
        e.updated = time.time()
        self._put("executions", e.id, e.model_dump())

    # outbox
    def list_events(self) -> list[OutboxEvent]:
        with self.lock:
            items = [OutboxEvent.model_validate(v) for v in self._load().get("events", {}).values()]
        return sorted(items, key=lambda e: e.created)

    def save_event(self, e: OutboxEvent) -> None:
        self._put("events", e.id, e.model_dump())

    # idempotency
    def idempotent(self, key: str) -> dict | None:
        with self.lock:
            hit = self._load().get("idempotency", {}).get(key)
        return hit.get("response") if hit else None

    def remember(self, key: str, response: dict) -> None:
        self._put("idempotency", key, {"created": time.time(), "response": response})
