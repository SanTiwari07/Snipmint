"""Use a video file from the person's computer instead of a YouTube link."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from ..audio import ffmpeg as ff
from ..errors import IngestError
from .source import Source, SourceMeta

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def max_upload_bytes() -> int:
    try:
        return int(float(os.environ.get("MAX_UPLOAD_MB") or 4096) * 1024 * 1024)
    except ValueError:
        return 4096 * 1024 * 1024


def safe_name(name: str) -> str:
    base = re.sub(r"[^\w.\- ]", "_", Path(name or "video").name).strip() or "video"
    return base[:120]


def _probe(path: Path) -> dict:
    cmd = [ff.find_ffprobe(), "-v", "error", "-show_entries",
           "format=duration:stream=codec_type,codec_name,width,height", "-of", "json", str(path)]  # fmt: skip
    proc = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW, check=False)
    if proc.returncode != 0:
        raise IngestError("That file could not be read as a video.")
    return json.loads(proc.stdout or b"{}")


def import_file(tmp: Path, original_name: str, base: Path | None = None) -> Source:
    """Register an uploaded file as a source (folder ``up-<hash>``) and return it.

    The same file uploaded twice maps to the same source, so finished steps are reused.
    """
    name = safe_name(original_name)
    suffix = Path(name).suffix.lower()
    if suffix not in VIDEO_SUFFIXES:
        tmp.unlink(missing_ok=True)
        raise IngestError("Upload a video file (mp4, mov, mkv, webm, m4v or avi).")
    info = _probe(tmp)
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        tmp.unlink(missing_ok=True)
        raise IngestError("That file has no video track.")
    if not any(s.get("codec_type") == "audio" for s in streams):
        tmp.unlink(missing_ok=True)
        raise IngestError("That file has no sound. Snipmint needs speech to find moments.")
    duration = float((info.get("format") or {}).get("duration") or 0)

    h = hashlib.sha1()
    with tmp.open("rb") as fh:
        h.update(fh.read(4 * 1024 * 1024))
    h.update(str(tmp.stat().st_size).encode())
    source = Source(f"up-{h.hexdigest()[:12]}", base)

    if not source.video.is_file():
        if suffix in (".mp4", ".mov", ".m4v"):
            shutil.move(str(tmp), source.video)
        else:  # make it playable in the browser and seekable: remux, or re-encode if needed
            try:
                ff.run_ffmpeg(["-v", "error", "-i", str(tmp), "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
                               "-movflags", "+faststart", str(source.video)])  # fmt: skip
            except Exception:
                ff.run_ffmpeg(["-v", "error", "-i", str(tmp), "-map", "0:v:0", "-map", "0:a:0",
                               "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                               "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(source.video)])  # fmt: skip
            tmp.unlink(missing_ok=True)
    else:
        tmp.unlink(missing_ok=True)

    if source.meta is None:
        source.save_meta(SourceMeta(
            id=source.id, url=f"file:{name}", title=Path(name).stem, channel="Uploaded file",
            duration=duration, width=video.get("width"), height=video.get("height"),
        ))  # fmt: skip
    source.set_stage("probe", "done")
    source.set_stage("download", "done")
    return source
