"""Which links Snipmint accepts: one YouTube video, nothing else.

Adapted from openshorts/yt_clients.py (``youtube_non_video_reason``) and
openshorts/ssrf_guard.py (MIT, (c) 2024 OpenShorts). The host allowlist means yt-dlp is
never pointed at an arbitrary site or a private address.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from ..errors import IngestError

YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com",
}  # fmt: skip
_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_SINGLE_VIDEO_PREFIXES = ("shorts", "live", "embed", "v", "e")
_NOT_ONE_VIDEO = {
    "results": "a search results page",
    "playlist": "a playlist",
    "hashtag": "a hashtag page",
    "channel": "a channel page",
    "c": "a channel page",
    "user": "a channel page",
    "feed": "a YouTube page with no video in it",
}


def youtube_video_id(url: str) -> str:
    """The 11-character video id of a single-video YouTube link.

    Raises :class:`IngestError` with a message the person can act on otherwise.
    """
    raw = (url or "").strip()
    if not raw:
        raise IngestError("Paste a YouTube link first.")
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except ValueError as exc:
        raise IngestError("That doesn't look like a link.") from exc
    if parsed.scheme not in ("http", "https"):
        raise IngestError("Only http(s) YouTube links are supported.")
    host = (parsed.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        raise IngestError("Only YouTube links are supported for now (youtube.com or youtu.be).")

    parts = [p for p in parsed.path.split("/") if p]
    if host.endswith("youtu.be"):
        vid = parts[0] if parts else ""
    elif parts and parts[0] == "watch" or not parts:
        vid = (parse_qs(parsed.query).get("v") or [""])[0]
        if not vid and "list" in parse_qs(parsed.query):
            raise IngestError("That's a playlist. Open one video in it and paste that link.")
    elif parts[0] in _SINGLE_VIDEO_PREFIXES and len(parts) >= 2:
        vid = parts[1]
    elif parts[0] in _NOT_ONE_VIDEO or parts[0].startswith("@"):
        what = _NOT_ONE_VIDEO.get(parts[0], "a channel page")
        raise IngestError(f"That's {what}, not a video. Open one video and paste its link.")
    else:
        vid = ""
    if not _ID.match(vid):
        raise IngestError("Couldn't find a video in that link. Copy the link from the video's Share button.")
    return vid


def canonical_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def source_id_for(video_id: str) -> str:
    return f"yt-{video_id}"
