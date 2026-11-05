"""One-pass ffmpeg rendering: background + ASS captions + narration (NarraReel NarraReel WHAT_TO_DO.md 5.11).

ffmpeg always runs with ``cwd`` = the job folder and *relative* file names. That avoids the
Windows drive-letter colon problem inside filter arguments (``subtitles=C:\\...`` breaks;
``subtitles=captions.ass`` works).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..audio.ffmpeg import has_libass, popen_ffmpeg, run_ffmpeg
from ..captions.colors import parse_hex
from ..errors import DependencyMissing, FFmpegError, JobCancelled

log = logging.getLogger("snipmint.render")

BackgroundType = Literal["solid", "transparent", "image", "video"]
Container = Literal["mp4", "webm", "mov"]
Quality = Literal["fast", "balanced", "high"]


@dataclass
class RenderSpec:
    width: int
    height: int
    fps: int
    duration: float
    background: BackgroundType = "solid"
    color: str = "#101010"
    background_file: str | None = None  # relative to the job folder
    blur: float = 0.0  # gaussian sigma for image/video backgrounds
    darken: float = 0.0  # 0..1, helps caption readability
    container: Container = "mp4"
    quality: Quality = "balanced"
    ass: str = "captions.ass"
    fonts_dir: str = "fonts"
    audio: str | None = "voice.wav"
    output: str | None = None


_X264 = {"fast": ("veryfast", 23), "balanced": ("medium", 20), "high": ("slow", 17)}
_VP9_CRF = {"fast": 36, "balanced": 31, "high": 26}


def _color_hex(value: str) -> str:
    r, g, b, _ = parse_hex(value)
    return f"0x{r:02X}{g:02X}{b:02X}"


def _bg_input(spec: RenderSpec, duration: float) -> tuple[list[str], str]:
    """(input args, filter chain applied to the background before captions)."""
    size = f"{spec.width}x{spec.height}"
    if spec.background == "transparent":
        return ["-f", "lavfi", "-i", f"color=c=black@0.0:s={size}:r={spec.fps}:d={duration:.3f},format=rgba"], ""
    if spec.background == "solid":
        return ["-f", "lavfi", "-i", f"color=c={_color_hex(spec.color)}:s={size}:r={spec.fps}:d={duration:.3f}"], ""
    if not spec.background_file:
        raise FFmpegError(f"A {spec.background} background needs a file.")
    fit = (
        f"scale={spec.width}:{spec.height}:force_original_aspect_ratio=increase,"
        f"crop={spec.width}:{spec.height},setsar=1"
    )
    extras = ""
    if spec.blur > 0:
        extras += f",gblur=sigma={spec.blur:g}"
    if spec.darken > 0:
        extras += f",eq=brightness={-min(1.0, spec.darken) * 0.5:.3f}"
    if spec.background == "image":
        args = ["-loop", "1", "-framerate", str(spec.fps), "-t", f"{duration:.3f}",
                "-i", spec.background_file]  # fmt: skip
    else:
        args = ["-stream_loop", "-1", "-t", f"{duration:.3f}", "-i", spec.background_file]
    return args, fit + extras + f",fps={spec.fps}"


def _subtitles_filter(spec: RenderSpec) -> str:
    f = f"subtitles={spec.ass}:fontsdir={spec.fonts_dir}"
    if spec.background == "transparent":
        f += ":alpha=1"
    return f


def build_command(spec: RenderSpec, output: str) -> list[str]:
    """The full ffmpeg argument list (without the leading program name)."""
    bg_args, bg_filter = _bg_input(spec, spec.duration)
    chain = [c for c in (bg_filter.lstrip(","), _subtitles_filter(spec)) if c]
    transparent = spec.background == "transparent"
    if transparent:
        chain.append("format=yuva420p" if spec.container == "webm" else "format=yuva444p10le")
    args = [*bg_args]
    if spec.audio:
        args += ["-i", spec.audio]
    args += ["-vf", ",".join(chain)]

    if spec.container == "mp4":
        if transparent:
            raise FFmpegError("MP4 cannot hold transparency. Use WebM or MOV for overlays.")
        preset, crf = _X264[spec.quality]
        args += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
                 "-movflags", "+faststart"]  # fmt: skip
        audio_codec = ["-c:a", "aac", "-b:a", "192k"]
    elif spec.container == "webm":
        pix = "yuva420p" if transparent else "yuv420p"
        args += ["-c:v", "libvpx-vp9", "-pix_fmt", pix, "-b:v", "0", "-crf",
                 str(_VP9_CRF[spec.quality]), "-auto-alt-ref", "0", "-row-mt", "1"]  # fmt: skip
        if spec.quality == "fast":
            args += ["-deadline", "realtime", "-cpu-used", "8"]
        audio_codec = ["-c:a", "libopus", "-b:a", "128k"]
    else:  # mov / ProRes 4444 for editors
        args += ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt",
                 "yuva444p10le" if transparent else "yuv422p10le"]  # fmt: skip
        audio_codec = ["-c:a", "pcm_s16le"]
    if spec.audio:
        args += [*audio_codec, "-shortest"]
    else:
        args += ["-an"]
    args += ["-t", f"{spec.duration:.3f}", "-r", str(spec.fps)]
    args += ["-progress", "pipe:1", "-nostats", output]
    return args


def render(
    job_dir: Path,
    spec: RenderSpec,
    output: str,
    *,
    progress: Callable[[float], None] | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    """Render the video into ``job_dir/output``. Progress is a 0..1 fraction of the duration."""
    if not has_libass():
        raise DependencyMissing(
            "libass support in ffmpeg",
            "Captions need an ffmpeg build with the 'subtitles' filter. "
            "Windows: `winget install Gyan.FFmpeg`; macOS: `brew install ffmpeg`.",
        )
    job_dir = Path(job_dir)
    (job_dir / output).unlink(missing_ok=True)
    args = build_command(spec, output)
    proc = popen_ffmpeg(args, cwd=job_dir)
    stderr_lines: list[str] = []

    def drain_stderr() -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            stderr_lines.append(raw.decode("utf-8", "replace").rstrip())
            del stderr_lines[:-60]

    t = threading.Thread(target=drain_stderr, daemon=True)
    t.start()

    def watch_cancel() -> None:
        while proc.poll() is None:
            if cancel is not None and cancel.wait(0.1):
                proc.terminate()
                return

    watcher = threading.Thread(target=watch_cancel, daemon=True)
    watcher.start()

    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").strip()
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                micros = int(line.split("=", 1)[1])
            except ValueError:
                continue
            if progress and spec.duration > 0:
                progress(min(1.0, max(0.0, micros / 1_000_000 / spec.duration)))
    code = proc.wait()
    t.join(timeout=2)
    if cancel is not None and cancel.is_set():
        (job_dir / output).unlink(missing_ok=True)
        raise JobCancelled("Cancelled.")
    if code != 0:
        raise FFmpegError(
            f"Rendering failed (ffmpeg exit code {code}).", "\n".join(stderr_lines[-30:])
        )
    if progress:
        progress(1.0)
    return job_dir / output


def render_frame(
    job_dir: Path, spec: RenderSpec, at: float, output: str = "preview.png"
) -> Path:
    """A single PNG frame at time ``at`` using exactly the same filter graph as the video."""
    job_dir = Path(job_dir)
    frame_spec = RenderSpec(**{**spec.__dict__, "duration": at + 0.5, "audio": None})
    bg_args, bg_filter = _bg_input(frame_spec, frame_spec.duration)
    chain = [c for c in (bg_filter.lstrip(","), _subtitles_filter(frame_spec)) if c]
    args = [*bg_args, "-vf", ",".join(chain), "-ss", f"{at:.3f}", "-frames:v", "1", output]
    run_ffmpeg(args, cwd=job_dir, timeout=60)
    return job_dir / output
