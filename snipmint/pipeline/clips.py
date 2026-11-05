"""Render the clips the person picked: cut, reframe, caption, encode.

One clip failing is logged and reported; the others still render.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .. import config
from ..analyze.schemas import Layout
from ..audio import ffmpeg as ff
from ..captions.ass import build_captions
from ..captions.fonts import copy_fonts_for_job
from ..captions.formats import VideoFormat, get_format
from ..captions.from_transcript import clip_words
from ..captions.srt import build_srt
from ..captions.styles import resolve_style
from ..errors import JobCancelled, ScriptError, SnipmintError
from ..ingest.source import Source
from ..jobs.workspace import Workspace
from ..models import ProgressEvent
from ..project import CaptionsConfig
from ..render.ffmpeg_render import RenderSpec, render, render_frame
from ..transcribe.models import Transcript

log = logging.getLogger("snipmint.clips")

ProgressCallback = Callable[[ProgressEvent], None]


class ClipSpec(BaseModel):
    id: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    title: str = ""
    hook_text: str = ""
    keywords: list[str] = Field(default_factory=list)
    descriptions: dict[str, str] = Field(default_factory=dict)


class ClipExport(BaseModel):
    quality: Literal["fast", "balanced", "high"] = "balanced"
    fps: int | None = Field(None, ge=10, le=60)


class ClipsRequest(BaseModel):
    source_id: str
    clips: list[ClipSpec] = Field(min_length=1, max_length=30)
    captions: CaptionsConfig = Field(default_factory=CaptionsConfig)
    captions_enabled: bool = True
    format: str = "reels"
    framing: Layout = "center"
    export: ClipExport = Field(default_factory=ClipExport)


class ClipOutput(BaseModel):
    id: str
    index: int
    title: str
    start: float
    end: float
    video: str | None = None
    srt: str | None = None
    error: str | None = None


def clip_name(index: int, ext: str) -> str:
    return f"clip_{index:02d}.{ext}"


def _spec(source: Source, fmt: VideoFormat, clip: ClipSpec, ass: str | None, export: ClipExport) -> RenderSpec:
    return RenderSpec(
        width=fmt.width, height=fmt.height, fps=export.fps or fmt.fps,  # type: ignore[arg-type]
        duration=round(clip.end - clip.start, 3), background="video",
        background_file=str(source.video.resolve()), background_start=clip.start,
        audio=None, audio_from_background=True, container="mp4", quality=export.quality,
        ass=ass or "", video_filter=None,
    )  # fmt: skip


def _render_no_captions(job_dir: Path, spec: RenderSpec, out: str, progress, cancel) -> Path:
    # an empty ASS file keeps the same filter graph (and therefore the same framing)
    empty = job_dir / "empty.ass"
    if not empty.is_file():
        empty.write_text("[Script Info]\nScriptType: v4.00+\n", encoding="utf-8")
    spec.ass = "empty.ass"
    return render(job_dir, spec, out, progress=progress, cancel=cancel)


def make_clips(
    req: ClipsRequest,
    ws: Workspace,
    *,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> dict:
    source = Source.existing(req.source_id)
    if source is None or not source.video.is_file() or not source.file("transcript.json").is_file():
        raise SnipmintError("That video isn't ready yet. Analyse it first.")
    meta = source.meta
    transcript = Transcript.model_validate(source.read_json("transcript.json"))
    fmt = get_format(req.format)
    if not fmt.is_video:
        raise ScriptError("Pick a video format for clips.")
    if not ff.has_libass():
        raise SnipmintError("This ffmpeg build cannot draw captions (no libass). Run `snipmint doctor`.")
    style = resolve_style(req.captions.preset, req.captions.overrides, req.captions.font)
    duration = meta.duration if meta else transcript.duration

    outputs: list[ClipOutput] = []
    n = len(req.clips)
    for i, clip in enumerate(req.clips, start=1):
        if cancel is not None and cancel.is_set():
            raise JobCancelled("Cancelled.")
        start = max(0.0, clip.start)
        end = min(duration or clip.end, clip.end)
        clip = clip.model_copy(update={"start": start, "end": end})
        out = ClipOutput(id=clip.id, index=i, title=clip.title, start=start, end=end)
        base, span = (i - 1) / n, 1.0 / n

        def emit(stage: str, frac: float, message: str, _base=base, _span=span, _i=i) -> None:
            if progress:
                progress(ProgressEvent(stage=stage, fraction=round(_base + _span * frac, 4),  # type: ignore[arg-type]
                                       message=message, detail={"clip": _i, "of": n}))  # fmt: skip

        try:
            if end - start < 1.0:
                raise ScriptError("This clip is shorter than a second.")
            emit("captions", 0.02, f"Clip {i} of {n}: building captions")
            analysis, words = clip_words(transcript.words, start, end, clip.keywords)
            srt_name = clip_name(i, "srt")
            ws.file(srt_name).write_text(build_srt(analysis, words) if words else "", encoding="utf-8")
            out.srt = srt_name
            ass_name: str | None = None
            if req.captions_enabled and words:
                captions = build_captions(words, analysis, style, fmt, req.captions.keep_emoji)
                ass_name = clip_name(i, "ass")
                ws.file(ass_name).write_text(captions.ass, encoding="utf-8")
                copy_fonts_for_job([captions.font], ws.path / "fonts")
            spec = _spec(source, fmt, clip, ass_name, req.export)
            video_name = clip_name(i, "mp4")

            def on_render(frac: float, _emit=emit, _i=i) -> None:
                _emit("render", 0.05 + 0.95 * frac, f"Clip {_i} of {n}: rendering {int(frac * 100)}%")

            if ass_name:
                render(ws.path, spec, video_name, progress=on_render, cancel=cancel)
            else:
                _render_no_captions(ws.path, spec, video_name, on_render, cancel)
            out.video = video_name
        except JobCancelled:
            raise
        except Exception as exc:  # keep going with the other clips
            log.exception("Clip %s failed", i)
            out.error = str(exc) or type(exc).__name__
            emit("render", 1.0, f"Clip {i} of {n} failed: {out.error}")
        outputs.append(out)

    titles = [
        {"file": o.video, "title": c.title, "hook": c.hook_text, "start": c.start, "end": c.end,
         "descriptions": c.descriptions, "keywords": c.keywords}
        for o, c in zip(outputs, req.clips, strict=True)
    ]  # fmt: skip
    ws.write_json("titles.json", {"source": meta.model_dump() if meta else {"id": req.source_id},
                                  "clips": titles})  # fmt: skip
    ws.write_json("clips.request.json", req.model_dump())
    made = [o for o in outputs if o.video]
    if not made:
        raise SnipmintError("None of the clips could be rendered: " + (outputs[0].error or "unknown error"))
    if progress:
        progress(ProgressEvent(stage="done", fraction=1.0, message=f"{len(made)} of {n} clips ready"))
    return {"clips": [o.model_dump() for o in outputs], "files": {"titles": "titles.json"}}


def build_zip(job_dir: Path) -> Path:
    """All clip outputs + titles.json in one archive (made on demand, then cached)."""
    out = job_dir / "clips.zip"
    names = sorted(p.name for p in job_dir.iterdir()
                   if p.name.startswith("clip_") and p.suffix in (".mp4", ".srt"))  # fmt: skip
    if (job_dir / "titles.json").is_file():
        names.append("titles.json")
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as zf:
        for name in names:
            zf.write(job_dir / name, arcname=name)
    return out


# ---------------------------------------------------------------- previews


class ClipPreviewRequest(BaseModel):
    source_id: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    keywords: list[str] = Field(default_factory=list)
    captions: CaptionsConfig = Field(default_factory=CaptionsConfig)
    format: str = "reels"
    at: float | None = None  # seconds into the clip; default: an early caption with 2+ words


def preview_clip_frame(req: ClipPreviewRequest) -> bytes:
    """PNG of one frame of the real clip with real captions, from the real renderer."""
    source = Source.existing(req.source_id)
    if source is None or not source.video.is_file() or not source.file("transcript.json").is_file():
        raise SnipmintError("That video isn't ready yet.")
    transcript = Transcript.model_validate(source.read_json("transcript.json"))
    fmt = get_format(req.format)
    if not fmt.is_video:
        raise ScriptError("Pick a video format for clips.")
    style = resolve_style(req.captions.preset, req.captions.overrides, req.captions.font)
    analysis, words = clip_words(transcript.words, req.start, req.end, req.keywords)
    at = req.at
    ass_text = "[Script Info]\nScriptType: v4.00+\n"
    font = None
    if words:
        captions = build_captions(words, analysis, style, fmt, req.captions.keep_emoji)
        ass_text, font = captions.ass, captions.font
        if at is None:
            chunk = next((c for c in captions.chunks if len(c.words) >= 2), captions.chunks[0])
            k = min(1, len(chunk.words) - 1)
            at = chunk.words[k].start + 0.04
    at = max(0.0, min(at or 0.5, req.end - req.start - 0.05))
    key = hashlib.sha1(f"{source.id}|{req.start:.3f}|{req.format}|{at:.3f}|{ass_text}".encode()).hexdigest()[:16]
    folder = config.cache_dir() / "clip_previews" / key
    out = folder / "preview.jpg"
    if not out.is_file():
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "captions.ass").write_text(ass_text, encoding="utf-8")
        if font is not None:
            copy_fonts_for_job([font], folder / "fonts")
        else:
            (folder / "fonts").mkdir(exist_ok=True)
        spec = RenderSpec(
            width=fmt.width, height=fmt.height, fps=fmt.fps, duration=req.end - req.start,  # type: ignore[arg-type]
            background="video", background_file=str(source.video.resolve()),
            background_start=req.start, audio=None, ass="captions.ass",
        )  # fmt: skip
        render_frame(folder, spec, at, "preview.jpg")
    return out.read_bytes()
