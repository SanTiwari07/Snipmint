"""Automation tasks: the business rules n8n triggers but never decides.

A task is one clip to publish (now, at a time, or as part of a batch). ``publish_task`` is the
only way automation starts an upload, and it is safe to call any number of times:

- it re-checks everything (automation on, task due, clip file present, metadata *approved*);
- it never starts a second upload for a task that has one running or finished;
- uploads go through the existing ``PublishManager`` (same duplicate protection, resumable
  uploads, OAuth) — automation adds no upload code of its own;
- failures get an exponential backoff (1 min, 2, 4 … capped at 1 h) and a hard attempt limit,
  and permanent failures (quota, rejected, signed out, missing file) are not retried.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ..ingest.source import Source
from ..metadata import generator
from ..metadata.schemas import MetadataRecord
from ..youtube.errors import DuplicateUpload, YouTubeError
from ..youtube.publisher import ClipToPublish, PublishRequest
from ..youtube.store import Privacy
from .events import EventDispatcher
from .settings import AutomationEnv
from .store import (
    OPEN,
    TERMINAL,
    AutomationSettings,
    AutomationStore,
    AutomationTask,
    Batch,
    ClipRef,
    Execution,
)

log = logging.getLogger("snipmint.automation")

PERMANENT_ERRORS = {
    "QuotaExceeded", "UploadRejected", "DuplicateUpload", "AuthError", "SecretError", "NotConnected",
    "NotConfigured", "OAuthStateError", "ClipMissing",
}  # fmt: skip
READY_GRACE = 120.0  # a READY task n8n hasn't picked up after this long is offered again by /due
STATUS_LABEL = {
    "AWAITING_APPROVAL": "Waiting for approval", "READY": "Processing", "SCHEDULED": "Scheduled",
    "PROCESSING": "Processing", "UPLOADING": "Uploading", "PUBLISHED": "Published",
    "FAILED": "Failed", "CANCELLED": "Cancelled", "SKIPPED": "Skipped",
}  # fmt: skip


class AutomationError(YouTubeError):
    http_status = 409


class AutomationOff(AutomationError):
    def __init__(self, message: str | None = None):
        super().__init__(message or "Automation is off. Set SNIPMINT_N8N_WEBHOOK_SECRET and turn automation on.")


class NotFound(AutomationError):
    http_status = 404


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def check_run_at(run_at: datetime, now: datetime | None = None) -> datetime:
    if run_at.tzinfo is None:
        raise AutomationError("The scheduled time needs a time zone (e.g. 2026-11-07T18:00:00+05:30).")
    now = now or datetime.now(timezone.utc)
    if run_at < now + timedelta(minutes=1):
        raise AutomationError("Pick a time at least a minute from now.")
    if run_at > now + timedelta(days=365):
        raise AutomationError("Pick a time within the next year.")
    return run_at.astimezone(timezone.utc)


def retry_delay(attempts: int) -> float:
    return min(3600.0, 60.0 * 2 ** max(0, attempts - 1))


class AutomationService:
    def __init__(self, env: AutomationEnv, store: AutomationStore, dispatcher: EventDispatcher, youtube,
                 clock=time.time):  # fmt: skip
        self.env = env
        self.store = store
        self.events = dispatcher
        self.youtube = youtube  # server.youtube_api.YouTubeIntegration (resolver + PublishManager)
        self.clock = clock
        self._lock = threading.RLock()

    # --------------------------------------------------------------- settings

    @property
    def settings(self) -> AutomationSettings:
        return self.store.get_settings()

    def update_settings(self, changes: dict[str, Any]) -> AutomationSettings:
        with self._lock:
            data = {**self.settings.model_dump(), **changes}
            generator.check_language(str(data.get("language", "en")))
            s = AutomationSettings.model_validate(data)
            self.store.save_settings(s)
            return s

    def require_on(self) -> AutomationSettings:
        if self.env.config_error:
            raise AutomationOff(self.env.config_error)
        if not self.env.configured:
            raise AutomationOff()
        s = self.settings
        if not s.enabled:
            raise AutomationOff("Automation is turned off. Turn it on on the Publish screen.")
        return s

    # ------------------------------------------------------------------ views

    def _record(self, clip: ClipRef) -> MetadataRecord | None:
        src = Source.existing(clip.source_id)
        return generator.load_all(src).get(clip.clip_id) if src else None

    def view(self, task: AutomationTask) -> dict[str, Any]:
        rec = self._record(task.clip)
        meta = rec.metadata if rec else None
        return {
            **task.model_dump(),
            "label": STATUS_LABEL.get(task.status, task.status),
            "has_metadata": rec is not None,
            "approved": bool(rec and rec.approved),
            "approved_by": rec.approved_by if rec else None,
            "metadata": None if meta is None else {
                "title": meta.title, "description": meta.description, "hashtags": meta.hashtags,
                "tags": meta.tags, "hook": meta.hook, "pinned_comment": meta.pinned_comment,
                "language": meta.language, "category_id": meta.category_id, "seo_keywords": meta.seo_keywords,
            },
            "due": self._is_due(task),
        }  # fmt: skip

    def _task(self, task_id: str) -> AutomationTask:
        t = self.store.get_task(task_id)
        if t is None:
            raise NotFound("That automation task doesn't exist.")
        return t

    def get(self, task_id: str) -> dict[str, Any]:
        return self.view(self._task(task_id))

    def _is_due(self, t: AutomationTask) -> bool:
        now = self.clock()
        if t.status == "SCHEDULED":
            return bool(t.run_at) and parse_utc(t.run_at).timestamp() <= now
        if t.status == "FAILED":
            return (t.next_attempt_at is not None and t.next_attempt_at <= now and t.attempts < t.max_attempts
                    and t.error_kind not in PERMANENT_ERRORS)  # fmt: skip
        if t.status == "READY":
            return now - t.created >= READY_GRACE
        if t.status == "AWAITING_APPROVAL":
            rec = self._record(t.clip)
            return bool(rec and rec.approved) and (not t.run_at or parse_utc(t.run_at).timestamp() <= now)
        return False

    def due(self, limit: int = 25) -> list[dict[str, Any]]:
        """What n8n's scheduler should publish now: due schedules, retries whose backoff is over,
        READY tasks n8n missed (e.g. it was down), and tasks approved since they were held."""
        if not self.env.configured or not self.settings.enabled:
            return []
        due = [t for t in self.store.list_tasks() if t.status in (*OPEN, "FAILED") and self._is_due(t)]
        due.sort(key=lambda t: (t.run_at or "", t.created))
        return [self.view(t) for t in due[:limit]]

    # ----------------------------------------------------------------- create

    def create(self, clips: list[ClipRef], *, privacy: Privacy | None = None, made_for_kids: bool | None = None,
               run_at: datetime | None = None, batch: bool = False, delay_seconds: int | None = None,
               concurrency: int | None = None, channel_id: str | None = None,
               language: str | None = None) -> dict[str, Any]:  # fmt: skip
        s = self.require_on()
        when = iso_utc(check_run_at(run_at)) if run_at else None
        seen: set[tuple[str, str]] = set()
        for c in clips:
            if (c.job_id, c.clip_file) in seen:
                raise AutomationError("The same clip is in the list twice.")
            seen.add((c.job_id, c.clip_file))
            self.youtube.resolve_clip(c.job_id, c.clip_file)  # the file must exist now
            if Source.existing(c.source_id) is None:
                raise AutomationError("That video isn't known here.")
        with self._lock:
            active = {(t.clip.job_id, t.clip.clip_file) for t in self.store.list_tasks() if t.status in OPEN}
            clash = seen & active
            if clash:
                raise AutomationError("One of these clips already has an automation task in progress.")
            kind = "batch" if batch and len(clips) > 1 else ("scheduled" if when else "publish")
            batch_obj = Batch(task_ids=[], delay_seconds=s.batch_delay_seconds if delay_seconds is None else delay_seconds,
                              concurrency=concurrency or s.batch_concurrency) if kind == "batch" else None  # fmt: skip
            tasks = []
            for c in clips:
                rec = self._record(c)
                t = AutomationTask(kind=kind, status="SCHEDULED" if when else "READY", clip=c,  # type: ignore[arg-type]
                                   title=rec.metadata.title if rec else "", channel_id=channel_id,
                                   privacy=privacy or s.privacy,
                                   made_for_kids=s.made_for_kids if made_for_kids is None else made_for_kids,
                                   language=language or s.language, run_at=when, max_attempts=s.max_attempts,
                                   batch_id=batch_obj.id if batch_obj else None)  # fmt: skip
                t.log(t.status, f"Scheduled for {when}" if when else "Created")
                self.store.save_task(t)
                tasks.append(t)
            if batch_obj:
                batch_obj.task_ids = [t.id for t in tasks]
                self.store.save_batch(batch_obj)
        # tell n8n (after saving: if n8n is down, the outbox keeps the event and /due catches up)
        if batch_obj:
            self.events.emit("batch.created", {"batch_id": batch_obj.id, "task_ids": batch_obj.task_ids,
                                               "delay_seconds": batch_obj.delay_seconds,
                                               "concurrency": batch_obj.concurrency})  # fmt: skip
        for t in tasks:
            if self._record(t.clip) is None:
                self.events.emit("metadata.requested", {"task_id": t.id, "language": t.language, **t.clip.model_dump()})
            elif not when and not batch_obj:
                self.events.emit("clip.ready", {"task_id": t.id, "title": t.title, **t.clip.model_dump()})
        return {"batch_id": batch_obj.id if batch_obj else None, "tasks": [self.view(t) for t in tasks]}

    # ---------------------------------------------------------------- publish

    def _clip_request(self, t: AutomationTask, rec: MetadataRecord) -> PublishRequest:
        m = rec.metadata
        lang = "hi" if m.language == "hinglish" else m.language
        clip = ClipToPublish(job_id=t.clip.job_id, clip_file=t.clip.clip_file, clip_id=t.clip.clip_id,
                             source_id=t.clip.source_id, title=m.title, description=m.description,
                             hashtags=m.hashtags, tags=m.tags, category_id=m.category_id,
                             language=lang if len(lang) <= 3 else None)  # fmt: skip
        return PublishRequest(channel_id=t.channel_id, clips=[clip], privacy=t.privacy,
                              made_for_kids=t.made_for_kids, confirmed=True)  # fmt: skip

    def _fail(self, t: AutomationTask, error: str, kind: str) -> None:
        t.status, t.error, t.error_kind = "FAILED", error[:500], kind
        retry = kind not in PERMANENT_ERRORS and t.attempts < t.max_attempts
        t.next_attempt_at = self.clock() + retry_delay(max(1, t.attempts)) if retry else None
        t.log("FAILED", error + (" (will retry)" if retry else ""))

    def publish(self, task_id: str, execution_id: str | None = None) -> dict[str, Any]:
        """Start the upload for a task if, and only if, everything allows it. Idempotent."""
        events: list[tuple[str, dict]] = []
        with self._lock:
            t = self._task(task_id)
            if execution_id and execution_id not in t.executions:
                t.executions = [*t.executions[-9:], execution_id]
            outcome = self._publish_locked(t, events)
            self.store.save_task(t)
            view = {**self.view(t), "outcome": outcome}
        for etype, payload in events:
            self.events.emit(etype, payload)
        self._check_batch(t.batch_id)
        return view

    def _publish_locked(self, t: AutomationTask, events: list) -> str:
        now = self.clock()
        if t.status in ("PROCESSING", "UPLOADING", "PUBLISHED"):
            return "already_started" if t.status != "PUBLISHED" else "already_published"
        if t.status in ("CANCELLED", "SKIPPED"):
            return "skipped"
        s = self.settings
        if not self.env.configured or not s.enabled:
            return "automation_off"
        if t.status == "FAILED":
            if t.attempts >= t.max_attempts or t.error_kind in PERMANENT_ERRORS:
                return "no_more_retries"
            if t.next_attempt_at and t.next_attempt_at > now:
                return "retry_later"
        was_scheduled = t.status == "SCHEDULED" or (t.status == "AWAITING_APPROVAL" and t.run_at)
        if t.run_at:
            run_at = parse_utc(t.run_at).timestamp()
            if run_at > now:
                return "not_due"
            if t.status == "SCHEDULED" and now - run_at > s.max_lateness_minutes * 60:
                t.status, t.error = "SKIPPED", f"Missed its time by more than {s.max_lateness_minutes} minutes."
                t.log("SKIPPED", t.error)
                return "missed"
        try:
            self.youtube.resolve_clip(t.clip.job_id, t.clip.clip_file)
        except YouTubeError as exc:
            t.attempts = t.max_attempts
            self._fail(t, str(exc), "ClipMissing")
            events.append(("upload.failed", self._payload(t)))
            return "failed"
        rec = self._record(t.clip)
        if rec is not None and not rec.approved and s.auto_approve:
            src = Source.existing(t.clip.source_id)
            try:
                rec = generator.set_approval(src, t.clip.clip_id, True, by="auto")  # type: ignore[arg-type]
                t.log("PROCESSING", "Metadata approved automatically (auto-approve is on).")
            except generator.MetadataError as exc:
                t.log("AWAITING_APPROVAL", str(exc))
        if rec is None or not rec.approved:
            if t.status != "AWAITING_APPROVAL":
                t.status = "AWAITING_APPROVAL"
                t.log("AWAITING_APPROVAL", "No metadata yet." if rec is None else "Metadata is not approved yet.")
                events.append(("metadata.awaiting_approval", self._payload(t)))
            return "awaiting_approval"

        t.title = rec.metadata.title
        t.status = "PROCESSING"
        manager = self.youtube.manager
        try:
            item_id = t.publish_item_id
            existing = manager.store.get_item(item_id) if item_id else None
            if existing is not None and existing.status in ("FAILED", "CANCELLED"):
                item = manager.retry(existing.id)  # resumes the same YouTube session when it can
            elif existing is not None:
                item = existing  # an upload already exists for this task: never start another
            else:
                _batch, items = manager.submit(self._clip_request(t, rec))
                item = items[0]
        except DuplicateUpload as exc:
            t.status, t.error, t.error_kind = "SKIPPED", str(exc), "DuplicateUpload"
            t.log("SKIPPED", str(exc))
            return "duplicate"
        except YouTubeError as exc:
            t.attempts += 1
            self._fail(t, str(exc), type(exc).__name__)
            events.append(("upload.failed", self._payload(t)))
            return "failed"
        t.attempts += 1
        t.publish_item_id, t.error, t.error_kind, t.next_attempt_at = item.id, None, None, None
        t.status = "UPLOADING"
        t.log("UPLOADING", f"Upload started (attempt {t.attempts}).")
        if was_scheduled:
            events.append(("schedule.triggered", self._payload(t)))
        return "started"

    def _payload(self, t: AutomationTask) -> dict[str, Any]:
        return {"task_id": t.id, "status": t.status, "label": STATUS_LABEL.get(t.status, t.status), "title": t.title,
                "video_url": t.video_url, "error": t.error, "attempts": t.attempts, "batch_id": t.batch_id,
                "run_at": t.run_at, "will_retry": t.next_attempt_at is not None, **t.clip.model_dump()}  # fmt: skip

    # ------------------------------------------------------- upload finished

    def on_item_finished(self, item) -> None:
        """PublishManager listener: mirror the upload's final state onto its task."""
        events: list[tuple[str, dict]] = []
        batch_id = None
        with self._lock:
            task = next((t for t in self.store.list_tasks() if t.publish_item_id == item.id), None)
            if task is None or task.status in TERMINAL and task.status != "FAILED":
                return
            if item.status == "PUBLISHED" and task.status != "PUBLISHED":
                task.status, task.video_url, task.error, task.error_kind = "PUBLISHED", item.video_url, None, None
                task.log("PUBLISHED", item.video_url or "")
                events.append(("upload.succeeded", self._payload(task)))
            elif item.status == "FAILED" and task.status != "FAILED":
                self._fail(task, item.error or "Upload failed.", item.error_kind or "YouTubeError")
                events.append(("upload.failed", self._payload(task)))
            elif item.status == "CANCELLED" and task.status != "CANCELLED":
                task.status = "CANCELLED"
                task.log("CANCELLED", "Upload cancelled.")
            else:
                return
            self.store.save_task(task)
            batch_id = task.batch_id
        for etype, payload in events:
            self.events.emit(etype, payload)
        self._check_batch(batch_id)

    # ---------------------------------------------------------------- batches

    @staticmethod
    def summarize(tasks: list[AutomationTask]) -> dict[str, Any]:
        def bucket(t: AutomationTask) -> str:
            if t.status == "PUBLISHED":
                return "succeeded"
            if t.status == "FAILED":
                return "pending" if t.next_attempt_at else "failed"
            if t.status in ("SKIPPED", "CANCELLED", "AWAITING_APPROVAL"):
                return "skipped"
            return "pending"

        out: dict[str, Any] = {"total": len(tasks), "succeeded": 0, "failed": 0, "pending": 0, "skipped": 0}
        for t in tasks:
            out[bucket(t)] += 1
        out["items"] = [{"task_id": t.id, "title": t.title, "status": t.status, "bucket": bucket(t),
                         "video_url": t.video_url, "error": t.error} for t in tasks]  # fmt: skip
        return out

    def batch(self, batch_id: str) -> dict[str, Any]:
        b = self.store.get_batch(batch_id)
        if b is None:
            raise NotFound("That batch doesn't exist.")
        tasks = [t for t in (self.store.get_task(i) for i in b.task_ids) if t]
        return {**b.model_dump(), "summary": self.summarize(tasks), "tasks": [self.view(t) for t in tasks]}

    def _check_batch(self, batch_id: str | None) -> None:
        if not batch_id:
            return
        with self._lock:
            b = self.store.get_batch(batch_id)
            if b is None or b.completed_at:
                return
            tasks = [t for t in (self.store.get_task(i) for i in b.task_ids) if t]
            summary = self.summarize(tasks)
            if summary["pending"]:
                return
            b.completed_at, b.summary = self.clock(), {k: v for k, v in summary.items() if k != "items"}
            self.store.save_batch(b)
        self.events.emit("batch.completed", {"batch_id": batch_id, **summary})

    # ---------------------------------------------------------------- metadata

    def ensure_metadata(self, task_id: str, language: str | None = None, gemini_key: str | None = None) -> dict:
        """Workflow D: generate missing metadata with the existing service, then hold for approval
        (or approve automatically when the person turned that on)."""
        self.require_on()
        events: list[tuple[str, dict]] = []
        with self._lock:
            t = self._task(task_id)
            src = Source.existing(t.clip.source_id)
            if src is None:
                raise NotFound("That video isn't known here.")
            rec = self._record(t.clip)
            if rec is None:
                ctx = generator.context_for(src, t.clip.clip_id, *self._clip_span(t))
                rec = generator.generate(src, ctx, language or t.language, gemini_key=gemini_key)
                t.log(t.status, f"Metadata written ({rec.provider}).")
            t.title = rec.metadata.title
            if not rec.approved and self.settings.auto_approve:
                try:
                    rec = generator.set_approval(src, t.clip.clip_id, True, by="auto")
                except generator.MetadataError as exc:
                    t.log(t.status, str(exc))
            if not rec.approved and t.status in ("READY", "AWAITING_APPROVAL"):
                if t.status != "AWAITING_APPROVAL":
                    events.append(("metadata.awaiting_approval", self._payload(t)))
                t.status = "AWAITING_APPROVAL"
                t.log("AWAITING_APPROVAL", "Waiting for you to approve the metadata.")
            elif rec.approved and t.status == "AWAITING_APPROVAL":
                t.status = "SCHEDULED" if t.run_at else "READY"
            self.store.save_task(t)
            view = self.view(t)
        for etype, payload in events:
            self.events.emit(etype, payload)
        return view

    def hold(self, task_id: str) -> dict[str, Any]:
        """Mark a task as waiting for approval unless its metadata is approved (workflow A, "no" branch)."""
        events: list[tuple[str, dict]] = []
        with self._lock:
            t = self._task(task_id)
            rec = self._record(t.clip)
            if t.status in ("READY", "SCHEDULED") and not (rec and rec.approved):
                t.status = "AWAITING_APPROVAL"
                t.log("AWAITING_APPROVAL", "No metadata yet." if rec is None else "Metadata is not approved yet.")
                events.append(("metadata.awaiting_approval", self._payload(t)))
                self.store.save_task(t)
            view = self.view(t)
        for etype, payload in events:
            self.events.emit(etype, payload)
        return view

    def _clip_span(self, t: AutomationTask) -> tuple[float, float]:
        folder = self.youtube.runner.workspace_for(t.clip.job_id)
        try:
            import json

            clips = json.loads((folder / "job.json").read_text(encoding="utf-8"))["result"]["clips"]
            c = next(c for c in clips if c.get("video") == t.clip.clip_file)
            return float(c["start"]), float(c["end"])
        except Exception as exc:
            raise AutomationError("Can't find this clip's time range in its job; make the clips again.") from exc

    # ------------------------------------------------------------ user actions

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            t = self._task(task_id)
            if t.status == "PUBLISHED":
                raise AutomationError("This clip is already on YouTube.")
            if t.status in ("CANCELLED", "SKIPPED"):
                return self.view(t)
            if t.status == "UPLOADING" and t.publish_item_id:
                self.youtube.manager.cancel(t.publish_item_id)
            t.status, t.next_attempt_at = "CANCELLED", None
            t.log("CANCELLED", "Cancelled by you.")
            self.store.save_task(t)
            view = self.view(t)
        self._check_batch(t.batch_id)
        return view

    def reschedule(self, task_id: str, run_at: datetime) -> dict[str, Any]:
        when = iso_utc(check_run_at(run_at))
        with self._lock:
            t = self._task(task_id)
            if t.status in ("PROCESSING", "UPLOADING", "PUBLISHED"):
                raise AutomationError("This clip is already uploading or published.")
            t.run_at, t.status, t.kind = when, "SCHEDULED", "scheduled" if t.kind != "batch" else "batch"
            t.error, t.error_kind, t.next_attempt_at = None, None, None
            t.log("SCHEDULED", f"Rescheduled for {when}")
            self.store.save_task(t)
            return self.view(t)

    def retry(self, task_id: str) -> dict[str, Any]:
        """Manual retry: a fresh attempt budget, then the normal publish path."""
        with self._lock:
            t = self._task(task_id)
            if t.status not in ("FAILED", "CANCELLED", "SKIPPED"):
                raise AutomationError("Only failed, cancelled or skipped tasks can be retried.")
            future = t.run_at and parse_utc(t.run_at).timestamp() > self.clock()
            t.status = "SCHEDULED" if future else "READY"
            t.attempts, t.error, t.error_kind, t.next_attempt_at = 0, None, None, None
            t.log(t.status, "Retry requested.")
            self.store.save_task(t)
        if not future:
            return self.publish(task_id)
        return self.get(task_id)

    # ------------------------------------------------------------- executions

    def record_execution(self, e: Execution) -> Execution:
        with self._lock:
            prior = next((x for x in self.store.list_executions() if x.id == e.id), None)
            if prior is not None:
                e.started = prior.started
            self.store.save_execution(e)
            if e.task_id:
                t = self.store.get_task(e.task_id)
                if t is not None and e.id not in t.executions:
                    t.executions = [*t.executions[-9:], e.id]
                    self.store.save_task(t)
        return e

    def status(self) -> dict[str, Any]:
        s = self.settings
        reachable, why = (False, None)
        if self.env.configured:
            reachable, why = self.events.health()
        tasks = self.store.list_tasks()
        counts: dict[str, int] = {}
        for t in tasks:
            counts[t.status] = counts.get(t.status, 0) + 1
        return {
            "configured": self.env.configured, "config_error": self.env.config_error,
            "webhook_url_set": bool(self.env.webhook_url), "reachable": reachable, "unreachable_reason": why,
            "settings": s.model_dump(), "task_counts": counts, "events": self.events.counts(),
        }  # fmt: skip
