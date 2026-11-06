"""YouTube Data API v3 over plain httpx: the channel lookup and the resumable videos.insert.

Resumable upload protocol (https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol):
1. POST .../upload/youtube/v3/videos?uploadType=resumable&part=snippet,status with the metadata.
   The ``Location`` header is the session URL.
2. PUT byte ranges to the session URL. ``308 Resume Incomplete`` + ``Range: bytes=0-N`` means
   "send from N+1"; ``200/201`` returns the video resource.
3. After an error, ``PUT`` with ``Content-Range: bytes */size`` asks where to continue.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from ..errors import JobCancelled
from .errors import (
    AuthError,
    QuotaExceeded,
    Retryable,
    UploadRejected,
    YouTubeError,
)
from .settings import YouTubeSettings
from .store import Channel, VideoDetails

log = logging.getLogger("snipmint.youtube")

RETRY_STATUS = {429, 500, 502, 503, 504}
QUOTA_REASONS = {"quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded", "rateLimitExceeded"}


def _wait(seconds: float, cancel: threading.Event | None) -> bool:
    if cancel is None:
        time.sleep(seconds)
        return False
    return cancel.wait(seconds)


class SessionExpired(YouTubeError):
    """The resumable session URL is gone (404/410): start a new session."""


def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/shorts/{video_id}"


def google_error(resp: httpx.Response) -> tuple[str, str]:
    """(reason, message) from a Google error body. Never includes request headers or tokens."""
    try:
        body = resp.json()
    except ValueError:
        return "", resp.text[:200]
    err = body.get("error")
    if isinstance(err, dict):
        reasons = [e.get("reason", "") for e in err.get("errors") or [] if isinstance(e, dict)]
        return (reasons[0] if reasons else str(err.get("status", ""))), str(err.get("message", ""))[:300]
    if isinstance(err, str):  # OAuth endpoints
        return err, str(body.get("error_description", ""))[:300]
    return "", ""


def raise_for(resp: httpx.Response, what: str) -> None:
    if resp.status_code < 400:
        return
    reason, message = google_error(resp)
    detail = f"{what}: {message or reason or resp.status_code}"
    if resp.status_code == 401:
        raise AuthError("YouTube rejected the sign-in. Connect the channel again.")
    if reason in QUOTA_REASONS and resp.status_code in (403, 429) and reason != "rateLimitExceeded":
        raise QuotaExceeded(
            "This Google Cloud project's YouTube upload quota is used up for today (the default is "
            "100 videos.insert calls a day; yours may differ, see Google Cloud console > Quotas). "
            "Try again after midnight Pacific time."
        )
    if resp.status_code in RETRY_STATUS or reason == "rateLimitExceeded":
        raise Retryable(detail)
    if resp.status_code in (404, 410) and what == "upload":
        raise SessionExpired(detail)
    raise UploadRejected(detail)


def details_body(d: VideoDetails) -> dict[str, Any]:
    snippet: dict[str, Any] = {"title": d.title, "description": d.description, "categoryId": d.category_id}
    if d.tags:
        snippet["tags"] = d.tags
    if d.language:
        snippet["defaultLanguage"] = d.language
        snippet["defaultAudioLanguage"] = d.language
    status: dict[str, Any] = {
        "privacyStatus": "private" if d.publish_at else d.privacy,
        "selfDeclaredMadeForKids": d.made_for_kids,
        "embeddable": True,
    }
    if d.publish_at:
        status["publishAt"] = d.publish_at
    return {"snippet": snippet, "status": status}


class YouTubeClient:
    def __init__(
        self,
        settings: YouTubeSettings,
        http: httpx.Client | None = None,
        *,
        wait: Callable[[float, threading.Event | None], bool] | None = None,
        jitter: Callable[[], float] = random.random,
    ):
        self.settings = settings
        self.http = http or httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0))
        # wait(seconds, cancel) -> True when cancelled meanwhile; injectable so tests don't sleep
        self.wait = wait or _wait
        self.jitter = jitter

    # ----------------------------------------------------------------- channel

    def my_channel(self, access_token: str) -> Channel:
        resp = self.http.get(
            f"{self.settings.endpoints.api}/channels",
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        raise_for(resp, "channels.list")
        items = resp.json().get("items") or []
        if not items:
            raise UploadRejected(
                "This Google account has no YouTube channel. Create one on youtube.com, then connect again."
            )
        ch = items[0]
        sn = ch.get("snippet") or {}
        thumbs = sn.get("thumbnails") or {}
        thumb = (thumbs.get("default") or thumbs.get("medium") or {}).get("url")
        return Channel(id=str(ch["id"]), title=str(sn.get("title", "")), custom_url=sn.get("customUrl"),
                       thumbnail=thumb)  # fmt: skip

    # ------------------------------------------------------------------ upload

    def backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter: 1, 2, 4 … capped at 64 s."""
        return min(64.0, 2.0 ** attempt) * (0.5 + 0.5 * self.jitter())

    def start_session(self, token: str, details: VideoDetails, size: int) -> str:
        resp = self.http.post(
            self.settings.endpoints.upload,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": "video/mp4",
            },
            json=details_body(details),
        )
        raise_for(resp, "videos.insert")
        location = resp.headers.get("location")
        if not location:
            raise Retryable("YouTube did not return an upload session.")
        return location

    @staticmethod
    def _offset(resp: httpx.Response) -> int:
        rng = resp.headers.get("range")  # "bytes=0-12345"
        if not rng or "-" not in rng:
            return 0
        return int(rng.rsplit("-", 1)[1]) + 1

    def query_offset(self, token: str, url: str, size: int) -> tuple[int, dict | None]:
        resp = self.http.put(url, headers={"Authorization": f"Bearer {token}", "Content-Length": "0",
                                           "Content-Range": f"bytes */{size}"})  # fmt: skip
        if resp.status_code in (200, 201):
            return size, resp.json()
        if resp.status_code == 308:
            return self._offset(resp), None
        raise_for(resp, "upload")
        raise Retryable(f"upload: unexpected status {resp.status_code}")

    def send_chunk(self, token: str, url: str, path: Path, offset: int, size: int) -> tuple[int, dict | None]:
        length = min(self.settings.chunk_bytes, size - offset)
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read(length)
        resp = self.http.put(
            url,
            content=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Length": str(len(data)),
                "Content-Range": f"bytes {offset}-{offset + len(data) - 1}/{size}",
                "Content-Type": "video/mp4",
            },
        )
        if resp.status_code in (200, 201):
            return size, resp.json()
        if resp.status_code == 308:
            return self._offset(resp), None
        raise_for(resp, "upload")
        raise Retryable(f"upload: unexpected status {resp.status_code}")

    def upload(
        self,
        path: Path,
        details: VideoDetails,
        *,
        token: Callable[[], str],
        refresh: Callable[[], str],
        session_url: str | None = None,
        on_session: Callable[[str | None], None] = lambda _u: None,
        on_progress: Callable[[int, int], None] = lambda _s, _t: None,
        on_retry: Callable[[int, float, str], None] = lambda _a, _w, _m: None,
        cancel: threading.Event | None = None,
    ) -> dict:
        """Upload ``path``; returns the video resource. Resumes ``session_url`` when given."""
        size = path.stat().st_size
        access = token()
        refreshed = False
        failures = 0
        url = session_url
        offset = 0
        need_query = url is not None

        while True:
            if cancel is not None and cancel.is_set():
                raise JobCancelled("Cancelled.")
            try:
                if url is None:
                    url = self.start_session(access, details, size)
                    on_session(url)
                    offset, need_query = 0, False
                if need_query:
                    offset, video = self.query_offset(access, url, size)
                    need_query = False
                    if video is not None:
                        return video
                    on_progress(offset, size)
                before = offset
                offset, video = self.send_chunk(access, url, path, offset, size)
                on_progress(offset, size)
                if video is not None:
                    return video
                if offset > before:
                    failures = 0
            except AuthError:
                if refreshed:
                    raise
                access, refreshed = refresh(), True
                need_query = url is not None
            except SessionExpired:
                url, need_query = None, False
                on_session(None)
                failures += 1
                if failures > self.settings.max_retries:
                    raise
            except (Retryable, httpx.TransportError) as exc:
                failures += 1
                if failures > self.settings.max_retries:
                    raise Retryable(f"Gave up after {failures - 1} retries: {exc}") from None
                wait = self.backoff(failures - 1)
                on_retry(failures, wait, str(exc)[:200])
                log.warning("YouTube upload hiccup (attempt %d), retrying in %.1fs", failures, wait)
                if self.wait(wait, cancel):
                    raise JobCancelled("Cancelled.") from None
                need_query = url is not None
