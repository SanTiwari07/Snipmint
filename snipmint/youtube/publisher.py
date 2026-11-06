"""Publishing queue: uploads rendered clips to YouTube with a concurrency limit.

Uploads run on their own small thread pool (not the render queue), so publishing never waits
behind a render and vice versa. Every item is persisted in the store before anything is sent,
so a restart picks interrupted uploads up again (resuming the same resumable session when
YouTube still has it). Each batch is registered as a runner job, so the existing
``/api/tasks/{id}/events`` SSE endpoint streams its progress.

Scheduled publishing = upload now as ``private`` with ``status.publishAt``: YouTube makes the
video public at that time, whether or not Snipmint is running.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from ..errors import JobCancelled, SnipmintError
from ..jobs.runner import Job, JobRunner
from ..models import ProgressEvent
from .client import YouTubeClient, video_url
from .errors import DuplicateUpload, NotConfigured, YouTubeError
from .oauth import OAuthService
from .settings import YouTubeSettings
from .store import ACTIVE, IntegrationStore, Privacy, PublishItem, VideoDetails

log = logging.getLogger("snipmint.youtube")

MIN_SCHEDULE_AHEAD = timedelta(minutes=15)
MAX_SCHEDULE_AHEAD = timedelta(days=365)
TAGS_LIMIT = 500  # YouTube counts the characters of all tags (+2 quotes for tags with spaces, +1 commas)

Resolver = Callable[[str, str], Path]


class ClipToPublish(BaseModel):
    job_id: str = Field(min_length=1, max_length=80)
    clip_file: str = Field(pattern=r"^clip_\d{2}\.mp4$")
    clip_id: str | None = Field(None, max_length=80)
    source_id: str | None = Field(None, max_length=80)
    title: str = Field(min_length=1, max_length=100)
    description: str = Field("", max_length=4800)
    hashtags: list[str] = Field(default_factory=list, max_length=15)
    tags: list[str] = Field(default_factory=list, max_length=60)
    category_id: str = Field("22", pattern=r"^\d{1,3}$")
    language: str | None = Field(None, pattern=r"^[a-zA-Z]{2,3}(-[A-Za-z0-9]{2,8})?$")


class PublishRequest(BaseModel):
    channel_id: str | None = None
    clips: list[ClipToPublish] = Field(min_length=1, max_length=30)
    privacy: Privacy = "private"
    made_for_kids: bool = False
    publish_at: datetime | None = Field(None, description="Schedule: the video goes public at this time (uploaded as private now).")
    allow_duplicate: bool = False
    confirmed: bool = Field(False, description="Set by the confirmation dialog; nothing is uploaded without it.")

    @field_validator("publish_at")
    @classmethod
    def _future(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        if v.tzinfo is None:
            raise ValueError("include a time zone (e.g. 2026-11-07T18:00:00+05:30)")
        now = datetime.now(timezone.utc)
        if v < now + MIN_SCHEDULE_AHEAD:
            raise ValueError("pick a time at least 15 minutes from now")
        if v > now + MAX_SCHEDULE_AHEAD:
            raise ValueError("pick a time within the next year")
        return v.astimezone(timezone.utc)


def clean_text(text: str) -> str:
    """YouTube rejects < and > in titles and descriptions."""
    return text.replace("<", "‹").replace(">", "›").strip()


def clean_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    total = 0
    for raw in tags:
        tag = re.sub(r"[<>,\"]", "", raw).strip().lstrip("#")[:100]
        if not tag or tag.lower() in seen:
            continue
        cost = len(tag) + (2 if " " in tag else 0) + (1 if out else 0)
        if total + cost > TAGS_LIMIT:
            break
        out.append(tag)
        seen.add(tag.lower())
        total += cost
    return out


def compose_details(clip: ClipToPublish, req: PublishRequest) -> VideoDetails:
    hashtags = []
    for h in clip.hashtags:
        h = re.sub(r"[^\w]", "", h.lstrip("#"), flags=re.UNICODE)
        if h and f"#{h}" not in hashtags:
            hashtags.append(f"#{h}")
    description = clean_text(clip.description)
    if hashtags:
        description = f"{description}\n\n{' '.join(hashtags)}".strip()
    title = clean_text(clip.title)[:100]
    if not title:
        raise YouTubeError("Every clip needs a title.")
    return VideoDetails(
        title=title,
        description=description[:5000],
        tags=clean_tags(clip.tags),
        category_id=clip.category_id,
        privacy="private" if req.publish_at else req.privacy,
        made_for_kids=req.made_for_kids,
        language=clip.language,
        publish_at=req.publish_at.strftime("%Y-%m-%dT%H:%M:%SZ") if req.publish_at else None,
    )


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


class PublishManager:
    def __init__(
        self,
        settings: YouTubeSettings,
        store: IntegrationStore,
        oauth: OAuthService,
        runner: JobRunner,
        resolver: Resolver,
        client: YouTubeClient | None = None,
    ):
        self.settings = settings
        self.store = store
        self.oauth = oauth
        self.runner = runner
        self.resolver = resolver
        self.client = client or oauth.client
        self._pool: ThreadPoolExecutor | None = None
        self._lock = threading.RLock()
        self._cancels: dict[str, threading.Event] = {}
        self._batches: dict[str, tuple[Job, list[str]]] = {}
        self._item_batch: dict[str, str] = {}
        self._stopping = False
        # called with each item once it reaches PUBLISHED / FAILED / CANCELLED (e.g. automation)
        self.listeners: list[Callable[[PublishItem], None]] = []
        self._user_cancelled: set[str] = set()

    @property
    def pool(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=self.settings.concurrency,
                                                thread_name_prefix="snipmint-upload")  # fmt: skip
            return self._pool

    def shutdown(self) -> None:
        with self._lock:
            self._stopping = True  # interrupted uploads keep their state and resume on the next start
            for ev in self._cancels.values():
                ev.set()
            pool, self._pool = self._pool, None
        if pool is not None:
            # items stay UPLOADING/QUEUED on disk and resume on the next start
            pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ submit

    def submit(self, req: PublishRequest) -> tuple[str, list[PublishItem]]:
        if not self.settings.configured:
            raise NotConfigured()
        if not req.confirmed:
            raise YouTubeError("Confirm the upload first.")
        account = self.oauth._account(req.channel_id)  # raises NotConnected
        prepared: list[tuple[ClipToPublish, Path, str, int]] = []
        for clip in req.clips:
            path = self.resolver(clip.job_id, clip.clip_file)
            prepared.append((clip, path, file_sha256(path), path.stat().st_size))

        with self._lock:
            if not req.allow_duplicate:
                taken = {
                    i.file_sha256: i for i in self.store.list_items()
                    if i.channel_id == account.id and (i.status in ACTIVE or i.status == "PUBLISHED")
                }  # fmt: skip
                seen: set[str] = set()
                for clip, _path, digest, _size in prepared:
                    prior = taken.get(digest)
                    if prior is not None:
                        where = prior.video_url or prior.status.lower()
                        raise DuplicateUpload(f"“{clip.title}” is already on this channel or on its way ({where}). "
                                              "Tick “Upload again anyway” to send it a second time.")  # fmt: skip
                    if digest in seen:
                        raise DuplicateUpload(f"“{clip.title}” is in this batch twice.")
                    seen.add(digest)

            job = self.runner.track("publish")
            items = [
                PublishItem(batch_id=job.id, channel_id=account.id, job_id=clip.job_id, clip_file=clip.clip_file,
                            clip_id=clip.clip_id, source_id=clip.source_id, file_sha256=digest, file_size=size,
                            details=compose_details(clip, req))  # fmt: skip
                for clip, _path, digest, size in prepared
            ]
            for item in items:
                self.store.save_item(item)
            self._start_batch(job, items)
        return job.id, items

    def _start_batch(self, job: Job, items: list[PublishItem]) -> None:
        self._batches[job.id] = (job, [i.id for i in items])
        for item in items:
            self._item_batch[item.id] = job.id
            self._cancels[item.id] = threading.Event()
            self._emit(item, f"Queued “{item.details.title}”")
            self.pool.submit(self._run, item.id)

    def resume_pending(self) -> int:
        """Re-queue uploads a restart interrupted. Called once at start-up."""
        if not self.settings.configured:
            return 0
        pending = [i for i in self.store.list_items() if i.status in ACTIVE]
        if not pending:
            return 0
        with self._lock:
            job = self.runner.track("publish")
            for item in pending:
                item.status = "QUEUED"
                item.batch_id = job.id
                item.note = "Picked up again after a restart."
                self.store.save_item(item)
            self._start_batch(job, pending)
        log.info("Resuming %d YouTube upload(s) after a restart", len(pending))
        return len(pending)

    # ------------------------------------------------------------- user actions

    def retry(self, item_id: str) -> PublishItem:
        with self._lock:
            item = self._item(item_id)
            if item.status not in ("FAILED", "CANCELLED"):
                raise YouTubeError("Only failed or cancelled uploads can be retried.")
            item.status, item.error, item.progress = "QUEUED", None, item.progress if item.upload_url else 0.0
            job = self.runner.track("publish")
            item.batch_id = job.id
            self.store.save_item(item)
            self._start_batch(job, [item])
            return item

    def cancel(self, item_id: str) -> PublishItem:
        with self._lock:
            item = self._item(item_id)
            if item.status == "PUBLISHED":
                if item.scheduled:
                    raise YouTubeError(
                        "This video is already on YouTube as a private video scheduled to go public. "
                        "To cancel the schedule, open it in YouTube Studio and change its visibility."
                    )
                raise YouTubeError("This video is already on YouTube.")
            if item.status in ("FAILED", "CANCELLED"):
                return item
            ev = self._cancels.get(item_id)
            self._user_cancelled.add(item_id)
            if ev is not None:
                ev.set()
            if item.status == "QUEUED" or ev is None:
                item.status, item.error = "CANCELLED", None
                self.store.save_item(item)
                self._emit(item, "Cancelled")
                self._check_batch(item)
                self._notify(item)
            return item

    def remove(self, item_id: str) -> None:
        with self._lock:
            item = self._item(item_id)
            if item.status in ACTIVE:
                raise YouTubeError("Cancel the upload before removing it from the list.")
            self.store.delete_item(item_id)

    def _item(self, item_id: str) -> PublishItem:
        item = self.store.get_item(item_id)
        if item is None:
            raise YouTubeError("That upload doesn't exist.")
        return item

    # ------------------------------------------------------------------ worker

    def _update(self, item_id: str, **changes) -> PublishItem:
        with self._lock:
            item = self._item(item_id)
            for k, v in changes.items():
                setattr(item, k, v)
            self.store.save_item(item)
            return item

    def _emit(self, item: PublishItem, message: str) -> None:
        batch = self._batches.get(self._item_batch.get(item.id, ""))
        if batch is None:
            return
        job, ids = batch
        items = [self.store.get_item(i) for i in ids]
        done = sum((i.progress if i and i.status not in ("FAILED", "CANCELLED") else 1.0) for i in items if i)
        job.add_event(ProgressEvent(
            stage="publish", fraction=round(done / max(1, len(ids)), 4), message=message,
            detail={"item_id": item.id, "status": item.status, "progress": item.progress,
                    "video_url": item.video_url, "error": item.error},
        ))  # fmt: skip

    def _check_batch(self, item: PublishItem) -> None:
        batch_id = self._item_batch.get(item.id)
        batch = self._batches.get(batch_id or "")
        if batch is None:
            return
        job, ids = batch
        items = [self.store.get_item(i) for i in ids]
        if any(i is not None and i.status in ACTIVE for i in items):
            return
        published = sum(1 for i in items if i and i.status == "PUBLISHED")
        failed = [i for i in items if i and i.status == "FAILED"]
        job.result = {"published": published, "failed": len(failed),
                      "cancelled": sum(1 for i in items if i and i.status == "CANCELLED")}  # fmt: skip
        if failed and not published:
            self.runner.complete(job, "failed", failed[0].error or "Upload failed.")
        else:
            self.runner.complete(job, "done")
        self._batches.pop(batch_id or "", None)

    def _run(self, item_id: str) -> None:
        cancel = self._cancels.get(item_id) or threading.Event()
        try:
            with self._lock:
                item = self._item(item_id)
                if item.status != "QUEUED":
                    return
                item = self._update(item_id, status="PREPARING", attempts=item.attempts + 1)
            self._emit(item, f"Preparing “{item.details.title}”")
            path = self.resolver(item.job_id, item.clip_file)
            if path.stat().st_size != item.file_size or file_sha256(path) != item.file_sha256:
                raise YouTubeError("The clip file changed after it was queued. Make the clips again and re-publish.")
            channel = item.channel_id
            self.oauth.access_token(channel)  # fails fast when the sign-in is gone

            item = self._update(item_id, status="UPLOADING")
            self._emit(item, f"Uploading “{item.details.title}”")
            last = [0.0]

            def progress(sent: int, total: int) -> None:
                frac = round(sent / max(1, total), 4)
                if frac - last[0] >= 0.01 or sent >= total:
                    last[0] = frac
                    it = self._update(item_id, progress=frac, bytes_sent=sent)
                    self._emit(it, f"Uploading “{it.details.title}” ({frac:.0%})")

            def on_retry(attempt: int, wait: float, why: str) -> None:
                it = self._update(item_id, note=f"Retrying (attempt {attempt}) in {wait:.0f} s: {why}")
                self._emit(it, it.note or "")

            video = self.client.upload(
                path, item.details,
                token=lambda: self.oauth.access_token(channel),
                refresh=lambda: self.oauth.access_token(channel, force=True),
                session_url=item.upload_url,
                on_session=lambda url: self._update(item_id, upload_url=url),
                on_progress=progress, on_retry=on_retry, cancel=cancel,
            )  # fmt: skip
            video_id = str(video.get("id") or "")
            if not video_id:
                raise YouTubeError("YouTube finished the upload but did not return a video id.")
            status = video.get("status") or {}
            if status.get("uploadStatus") in ("rejected", "failed"):
                why = status.get("rejectionReason") or status.get("failureReason") or "unknown reason"
                raise YouTubeError(f"YouTube rejected the video ({why}).")
            note = None
            wanted = item.details.privacy
            got = status.get("privacyStatus")
            if got and got != wanted:
                # report what YouTube actually did; never claim the requested visibility
                note = (f"You asked for {wanted}, but YouTube set this video to {got}. Check it in YouTube "
                        "Studio (channel defaults or a policy review can change the visibility).")  # fmt: skip
            elif item.scheduled:
                note = f"Private until {item.details.publish_at} (UTC), then public."
            item = self._update(item_id, status="PUBLISHED", progress=1.0, video_id=video_id,
                                video_url=video_url(video_id), upload_url=None, error=None, note=note)  # fmt: skip
            self._emit(item, f"Published “{item.details.title}”")
        except JobCancelled:
            if self._stopping and item_id not in self._user_cancelled:
                log.info("Upload %s interrupted by shutdown; it resumes on the next start", item_id)
                return
            item = self._update(item_id, status="CANCELLED", error=None, upload_url=None)
            self._emit(item, f"Cancelled “{item.details.title}”")
        except SnipmintError as exc:
            item = self._update(item_id, status="FAILED", error=str(exc)[:500], error_kind=type(exc).__name__)
            log.info("Upload %s failed: %s", item_id, exc)
            self._emit(item, f"Failed: {exc}")
        except Exception as exc:  # never kill the worker thread silently
            log.exception("Upload %s crashed", item_id)
            item = self._update(item_id, status="FAILED", error=f"Unexpected error: {type(exc).__name__}",
                                error_kind="Unexpected")  # fmt: skip
            self._emit(item, item.error or "Failed")
        finally:
            with self._lock:
                self._cancels.pop(item_id, None)
                current = self.store.get_item(item_id)
                if current is not None:
                    self._check_batch(current)
            if current is not None and current.status not in ACTIVE:
                self._notify(current)

    def _notify(self, item: PublishItem) -> None:
        for listener in list(self.listeners):
            try:
                listener(item)
            except Exception:  # a listener must never break uploads
                log.exception("Publish listener failed for %s", item.id)
