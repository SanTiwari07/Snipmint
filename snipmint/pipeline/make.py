"""Full video pipeline: script -> narration -> captions -> rendered video (section 5.11-5.12)."""

from __future__ import annotations

import hashlib
import logging
import shutil
import threading
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from .. import config
from ..audio import ffmpeg as ff
from ..audio.process import read_wav
from ..captions.ass import build_captions
from ..captions.fonts import copy_fonts_for_job
from ..captions.formats import get_format
from ..captions.srt import build_srt
from ..captions.styles import resolve_style
from ..engines.base import TTSEngine
from ..errors import ScriptError, SnipmintError
from ..jobs.workspace import Workspace
from ..models import ProgressEvent, WordTiming
from ..project import ProjectConfig
from ..render.ffmpeg_render import RenderSpec, render, render_frame
from ..script import analyze_script
from ..script import duration as dur
from ..timing.estimate import estimate_script_timings
from .cache import SentenceCache
from .speak import speak

log = logging.getLogger("snipmint.make")

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}

ProgressCallback = Callable[[ProgressEvent], None]


class MakeResult(BaseModel):
    folder: Path
    video: Path | None = None
    audio_wav: Path
    audio_mp3: Path
    captions_srt: Path
    captions_ass: Path | None = None
    words_json: Path
    project_file: Path
    duration: float
    target_seconds: float | None = None
    target_met: bool | None = None
    note: str | None = None
    warnings: list[str] = []


def export_name(script: str, fmt: str, ext: str) -> str:
    """``snipmint_<first-3-words>_<format>.<ext>``"""
    words = []
    for raw in script.lower().split():
        # keep letters, digits and combining marks (Devanagari vowel signs are marks)
        word = "".join(ch for ch in raw if ch.isalnum() or unicodedata.category(ch).startswith("M"))
        if word:
            words.append(word)
    slug = "-".join(words[:3]) or "video"
    return f"snipmint_{slug}_{fmt}.{ext}"


def _stage(cb: ProgressCallback | None, stage: str, fraction: float, message: str) -> None:
    if cb:
        cb(ProgressEvent(stage=stage, fraction=min(1.0, max(0.0, fraction)), message=message))  # type: ignore[arg-type]


def _stage_progress(
    cb: ProgressCallback | None, lo: float, hi: float
) -> ProgressCallback | None:
    """Map a sub-pipeline's 0..1 events into [lo, hi] of the overall progress."""
    if cb is None:
        return None

    def mapped(ev: ProgressEvent) -> None:
        if ev.stage == "done":
            return
        cb(ev.model_copy(update={"fraction": lo + (hi - lo) * ev.fraction}))

    return mapped


def _prepare_background(project: ProjectConfig, ws: Workspace) -> str | None:
    bg = project.background
    if bg.type not in ("image", "video"):
        return None
    if not bg.path:
        raise ScriptError(f"Choose a {bg.type} file for the background.")
    src = Path(bg.path).expanduser()
    if not src.is_file():
        raise ScriptError(f"Background file not found: {src}")
    allowed = IMAGE_EXT if bg.type == "image" else VIDEO_EXT
    if src.suffix.lower() not in allowed:
        raise ScriptError(
            f"A {bg.type} background must be one of: {', '.join(sorted(allowed))}."
        )
    name = f"bg{src.suffix.lower()}"
    shutil.copyfile(src, ws.file(name))
    return name


def _read_words(ws: Workspace) -> list[WordTiming]:
    return [WordTiming.model_validate(w) for w in ws.read_json("words.json")]


def make_video(
    project: ProjectConfig,
    workspace: Workspace | Path,
    *,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    engine: TTSEngine | None = None,
    cache: SentenceCache | None | Literal[False] = None,
    reuse_audio: bool = False,
) -> MakeResult:
    """Run the whole pipeline. With ``reuse_audio`` the narration in the workspace is kept
    and only captions and video are rebuilt (fast restyling)."""
    ws = workspace if isinstance(workspace, Workspace) else Workspace(workspace)
    analysis = analyze_script(project.script)
    if analysis.word_count == 0:
        raise ScriptError("Your script is empty. Paste or type some text first.")
    fmt = get_format(project.format)
    if fmt.is_video and not ff.has_libass():
        raise SnipmintError(
            "This ffmpeg build cannot draw captions (no libass). Run `snipmint doctor` for help."
        )
    style = resolve_style(project.captions.preset, project.captions.overrides, project.captions.font)

    # ---- narration
    if reuse_audio and ws.file("voice.wav").is_file() and ws.file("words.json").is_file():
        _stage(progress, "voice", 0.65, "Reusing narration")
        words = _read_words(ws)
        samples, sr = read_wav(ws.file("voice.wav"))
        duration = samples.size / sr
        stored = ws.read_json("project.json") if ws.file("project.json").is_file() else {}
        target_met, note = stored.get("target_met"), stored.get("note")
    else:
        d = project.duration
        result = speak(
            analysis,
            project.voice.model_copy(update={"speed": d.speed or project.voice.speed}),
            ws,
            target_seconds=d.target_seconds,
            pause_scale=d.pause_scale,
            plan_option="speed_only" if d.option == "speed_only" else "adjust_delivery",
            loudness=project.export.loudness_lufs,
            progress=_stage_progress(progress, 0.0, 0.65),
            cancel=cancel,
            engine=engine,
            cache=cache,
        )
        words, duration = result.words, result.duration
        target_met, note = result.target_met, result.note
        ws_proj = ws.read_json("project.json")
        ws_proj["note"] = note
        ws.write_json("project.json", ws_proj)

    # ---- captions
    _stage(progress, "captions", 0.67, "Building captions")
    warnings: list[str] = []
    ws.file("captions.srt").write_text(build_srt(analysis, words), encoding="utf-8")
    ass_path: Path | None = None
    video: Path | None = None
    if fmt.is_video:
        captions = build_captions(words, analysis, style, fmt, project.captions.keep_emoji)
        warnings.extend(captions.warnings)
        ass_path = ws.file("captions.ass")
        ass_path.write_text(captions.ass, encoding="utf-8")
        copy_fonts_for_job([captions.font], ws.path / "fonts")
        bg_file = _prepare_background(project, ws)
        bg = project.background
        spec = RenderSpec(
            width=fmt.width, height=fmt.height, fps=project.export.fps or fmt.fps,  # type: ignore[arg-type]
            duration=duration, background=bg.type, color=bg.color, background_file=bg_file,
            blur=bg.blur, darken=bg.darken, container=project.export.video,
            quality=project.export.quality,
        )  # fmt: skip
        out_name = f"output.{project.export.video}"
        sub = _stage_progress(progress, 0.70, 1.0)

        def on_render(fraction: float) -> None:
            if sub:
                sub(ProgressEvent(stage="render", fraction=fraction,
                                  message=f"Rendering video ({int(fraction * 100)}%)"))  # fmt: skip

        video = render(ws.path, spec, out_name, progress=on_render, cancel=cancel)

    project_file = project.save(ws.file("project.snipmint.json"))
    _stage(progress, "done", 1.0, "Done")
    return MakeResult(
        folder=ws.path, video=video, audio_wav=ws.file("voice.wav"), audio_mp3=ws.file("voice.mp3"),
        captions_srt=ws.file("captions.srt"), captions_ass=ass_path, words_json=ws.file("words.json"),
        project_file=project_file, duration=duration, target_seconds=project.duration.target_seconds,
        target_met=target_met, note=note, warnings=warnings,
    )  # fmt: skip


# ---------------------------------------------------------------- previews


def preview_frame(
    project: ProjectConfig, *, at: float | None = None, word_index: int | None = None
) -> bytes:
    """PNG of one frame with real captions from the script, rendered by the real renderer.

    Uses estimated timings (no voice needed), so it is instant to update while the user tweaks
    styles. ``at`` is a time in seconds; by default the middle of an early caption chunk.
    """
    fmt = get_format(project.format)
    if not fmt.is_video:
        raise ScriptError("Audio-only exports have no caption preview.")
    analysis = analyze_script(project.script)
    if analysis.word_count == 0:
        raise ScriptError("Your script is empty. Paste or type some text first.")
    style = resolve_style(project.captions.preset, project.captions.overrides, project.captions.font)
    cps, _ = dur.get_cps(project.voice.ref, analysis.language_hint)
    words, total = estimate_script_timings(
        analysis, cps, project.duration.speed or project.voice.speed,
        project.duration.pause_scale or 1.0,
    )  # fmt: skip
    captions = build_captions(words, analysis, style, fmt, project.captions.keep_emoji)

    if at is None:
        chunks = captions.chunks
        chunk = next((c for c in chunks if len(c.words) >= 2), chunks[0])
        k = min(word_index if word_index is not None else 1, len(chunk.words) - 1)
        at = chunk.words[k].start + 0.04
    key = hashlib.sha1(
        (captions.ass + project.format + project.background.model_dump_json() + f"{at:.3f}").encode()
    ).hexdigest()[:16]
    folder = config.cache_dir() / "previews" / key
    out = folder / "preview.png"
    if not out.is_file():
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "captions.ass").write_text(captions.ass, encoding="utf-8")
        copy_fonts_for_job([captions.font], folder / "fonts")
        bg = project.background
        bg_file = None
        bg_type, bg_color = bg.type, bg.color
        if bg.type in ("image", "video"):
            bg_file = _prepare_background(project, Workspace(folder))
        elif bg.type == "transparent":
            bg_type, bg_color = "solid", "#5A6270"  # show overlays on a neutral backdrop
        spec = RenderSpec(
            width=fmt.width, height=fmt.height, fps=fmt.fps, duration=total,  # type: ignore[arg-type]
            background=bg_type, color=bg_color,
            background_file=bg_file, blur=bg.blur, darken=bg.darken,
        )  # fmt: skip
        render_frame(folder, spec, at, "preview.png")
    return out.read_bytes()
