"""Probe and download one YouTube video with yt-dlp.

Adapted from openshorts/main.py ``_download_youtube_video`` and openshorts/yt_clients.py
(MIT, (c) 2024 OpenShorts), without the proxy chain, paid-proxy ledger and cloud metering.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..errors import IngestError, JobCancelled
from .guard import canonical_url, source_id_for, youtube_video_id
from .source import Chapter, HeatPoint, SourceMeta

log = logging.getLogger("snipmint.ingest")

# Explicit client list: see openshorts/yt_clients.py for why the defaults alone fail on some videos.
HD_CLIENTS = ["default", "mweb"]
FORMAT = (
    "bestvideo[vcodec^=avc1][height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
    "bestvideo[height<=1080]+bestaudio/"
    "best[height<=1080][ext=mp4]/best[ext=mp4]/best"
)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _yt_dlp():
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover - core dependency
        raise IngestError("yt-dlp is not installed. Run `pip install yt-dlp`.") from exc
    return yt_dlp


def _cookies() -> str | None:
    path = (os.environ.get("YOUTUBE_COOKIES_FILE") or "").strip()
    return path if path and Path(path).is_file() else None


def _opts(**extra: Any) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "cachedir": False,
        "extractor_args": {"youtube": {"player_client": HD_CLIENTS}},
        "http_headers": {"User-Agent": _UA},
    }
    cookies = _cookies()
    if cookies:
        opts["cookiefile"] = cookies
    opts.update(extra)
    return opts


def friendly_error(exc: Exception) -> str:
    """Turn a yt-dlp error into one sentence the person can act on."""
    text = str(exc)
    low = text.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return ("YouTube asked for a sign-in check. Add a cookies file "
                "(YOUTUBE_COOKIES_FILE) and try again.")  # fmt: skip
    if "private video" in low:
        return "This video is private."
    if "age" in low and ("restrict" in low or "confirm your age" in low):
        return "This video is age-restricted. Add a cookies file from a signed-in account."
    if "members-only" in low or "join this channel" in low:
        return "This video is for channel members only."
    if "is not available" in low or "video unavailable" in low:
        return "This video is unavailable (removed, region-locked or private)."
    if "live event" in low or "is live" in low:
        return "Live streams can't be clipped until they have finished."
    return f"Couldn't fetch the video: {text.splitlines()[0][:200]}"


def meta_from_info(info: dict[str, Any], url: str) -> SourceMeta:
    vid = info.get("id") or youtube_video_id(url)
    duration = float(info.get("duration") or 0.0)
    chapters = [
        Chapter(start=float(c.get("start_time") or 0), end=float(c.get("end_time") or duration),
                title=str(c.get("title") or "").strip())
        for c in (info.get("chapters") or [])
    ]  # fmt: skip
    heat = [
        HeatPoint(start=float(h["start_time"]), end=float(h["end_time"]), value=float(h["value"]))
        for h in (info.get("heatmap") or [])
        if "start_time" in h and "end_time" in h and "value" in h
    ]
    return SourceMeta(
        id=source_id_for(vid), url=canonical_url(vid), video_id=vid,
        title=str(info.get("title") or ""), channel=str(info.get("channel") or info.get("uploader") or ""),
        duration=duration, width=info.get("width"), height=info.get("height"),
        thumbnail=info.get("thumbnail"), chapters=chapters, heatmap=heat,
        language=info.get("language"),
    )  # fmt: skip


def probe(url: str) -> SourceMeta:
    """Title, duration, chapters and the "most replayed" heatmap, without downloading."""
    vid = youtube_video_id(url)
    yt_dlp = _yt_dlp()
    try:
        with yt_dlp.YoutubeDL(_opts(skip_download=True)) as ydl:
            info = ydl.extract_info(canonical_url(vid), download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise IngestError(friendly_error(exc)) from exc
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise IngestError("Live streams can't be clipped until they have finished.")
    return meta_from_info(info, url)


def download(
    url: str,
    dest: Path,
    *,
    progress: Callable[[float, str], None] | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    """Download the video (<=1080p, mp4) to ``dest`` (a .mp4 path)."""
    vid = youtube_video_id(url)
    yt_dlp = _yt_dlp()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    stem = dest.with_suffix("")
    parts = {"done": 0}

    def hook(d: dict[str, Any]) -> None:
        if cancel is not None and cancel.is_set():
            raise JobCancelled("Cancelled.")
        if d.get("status") == "downloading" and progress:
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            got = d.get("downloaded_bytes") or 0
            frac = (got / total) if total else 0.0
            # video and audio are separate files: first half, second half
            overall = min(0.99, (parts["done"] + frac) / 2)
            speed = d.get("_speed_str") or ""
            progress(overall, f"Downloading video {int(overall * 100)}% {speed}".strip())
        elif d.get("status") == "finished":
            parts["done"] = min(parts["done"] + 1, 2)

    opts = _opts(
        format=FORMAT,
        merge_output_format="mp4",
        outtmpl=str(stem) + ".%(ext)s",
        overwrites=True,
        progress_hooks=[hook],
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([canonical_url(vid)])
    except JobCancelled:
        raise
    except yt_dlp.utils.DownloadError as exc:
        if cancel is not None and cancel.is_set():
            raise JobCancelled("Cancelled.") from exc
        raise IngestError(friendly_error(exc)) from exc
    if not dest.is_file():
        found = sorted(stem.parent.glob(stem.name + ".*"))
        found = [f for f in found if f.suffix in (".mp4", ".mkv", ".webm") and ".part" not in f.name]
        if not found:
            raise IngestError("The download finished but no video file was written.")
        found[0].replace(dest)
    if progress:
        progress(1.0, "Video downloaded")
    return dest
