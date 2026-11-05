"""The one place that finds and runs ffmpeg / ffprobe (NarraReel NarraReel WHAT_TO_DO.md section 4.3, rule 3)."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

from .. import config
from ..errors import DependencyMissing, FFmpegError

log = logging.getLogger("snipmint.ffmpeg")

_INSTALL_HINT = (
    "Install ffmpeg (with libass): Windows `winget install Gyan.FFmpeg` or `choco install ffmpeg`, "
    "macOS `brew install ffmpeg`, Linux `sudo apt install ffmpeg`. "
    "Or point SNIPMINT_FFMPEG at the ffmpeg binary."
)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


@lru_cache(maxsize=1)
def find_ffmpeg() -> str:
    """ffmpeg path: SNIPMINT_FFMPEG -> PATH -> imageio-ffmpeg (if installed)."""
    wanted = config.load_settings().ffmpeg
    if wanted:
        if Path(wanted).is_file() or shutil.which(wanted):
            return wanted
        raise DependencyMissing(f"ffmpeg at '{wanted}'", _INSTALL_HINT)
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise DependencyMissing("ffmpeg", _INSTALL_HINT) from None


@lru_cache(maxsize=1)
def find_ffprobe() -> str:
    wanted = config.load_settings().ffprobe
    if wanted:
        return wanted
    found = shutil.which("ffprobe")
    if found:
        return found
    sibling = Path(find_ffmpeg()).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if sibling.is_file():
        return str(sibling)
    raise DependencyMissing("ffprobe", _INSTALL_HINT)


def _tail(data: bytes | str, lines: int = 30) -> str:
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    return "\n".join(text.strip().splitlines()[-lines:])


def run_ffmpeg(
    args: Sequence[str | os.PathLike[str]],
    *,
    input: bytes | None = None,
    cwd: str | os.PathLike[str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run ffmpeg quietly; raise :class:`FFmpegError` with the stderr tail on failure."""
    cmd = [find_ffmpeg(), "-hide_banner", "-nostdin", "-y", *map(str, args)]
    log.debug("ffmpeg %s", " ".join(cmd[1:]))
    try:
        proc = subprocess.run(
            cmd,
            input=input,
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"ffmpeg timed out after {timeout:.0f}s.") from exc
    except OSError as exc:
        raise FFmpegError(f"Could not start ffmpeg: {exc}") from exc
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg failed (exit code {proc.returncode}).", _tail(proc.stderr))
    return proc


def popen_ffmpeg(
    args: Sequence[str | os.PathLike[str]], *, cwd: str | os.PathLike[str] | None = None
) -> subprocess.Popen[bytes]:
    """Start ffmpeg without waiting (used for rendering with progress and cancellation)."""
    cmd = [find_ffmpeg(), "-hide_banner", "-nostdin", "-y", *map(str, args)]
    log.debug("ffmpeg %s", " ".join(cmd[1:]))
    return subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )


def probe_duration(path: str | os.PathLike[str]) -> float:
    """Duration of a media file in seconds."""
    cmd = [
        find_ffprobe(),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1",
        str(path),
    ]  # fmt: skip
    proc = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe could not read {path}.", _tail(proc.stderr))
    try:
        return float(proc.stdout.decode().strip())
    except ValueError as exc:
        raise FFmpegError(f"ffprobe returned no duration for {path}.") from exc


def version() -> str:
    proc = subprocess.run([find_ffmpeg(), "-version"], capture_output=True, creationflags=_NO_WINDOW)
    first = proc.stdout.decode("utf-8", "replace").splitlines()[:1]
    m = re.match(r"ffmpeg version (\S+)", first[0]) if first else None
    return m.group(1) if m else "unknown"


@lru_cache(maxsize=1)
def has_libass() -> bool:
    """True if this ffmpeg can burn ASS subtitles (needed for captions)."""
    proc = subprocess.run(
        [find_ffmpeg(), "-hide_banner", "-filters"],
        capture_output=True,
        creationflags=_NO_WINDOW,
    )
    text = proc.stdout.decode("utf-8", "replace")
    return bool(re.search(r"\bsubtitles\b", text)) and bool(re.search(r"\bass\b", text))


def available() -> bool:
    try:
        find_ffmpeg()
        find_ffprobe()
        return True
    except DependencyMissing:
        return False
