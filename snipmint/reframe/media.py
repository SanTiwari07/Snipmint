"""Small ffmpeg helpers for reframing: video size, black-bar detection, frame sampling."""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..audio import ffmpeg as ff

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class Box:
    """A rectangle in source pixels."""

    w: int
    h: int
    x: int = 0
    y: int = 0

    def filter(self) -> str:
        return f"crop={self.w}:{self.h}:{self.x}:{self.y}"


def video_size(path: Path) -> tuple[int, int]:
    """Display width and height of the first video stream (rotation applied)."""
    cmd = [ff.find_ffprobe(), "-v", "error", "-select_streams", "v:0", "-show_entries",
           "stream=width,height:stream_side_data=rotation", "-of", "json", str(path)]  # fmt: skip
    proc = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW, check=False)
    data = json.loads(proc.stdout or b"{}")
    stream = (data.get("streams") or [{}])[0]
    w, h = int(stream.get("width") or 0), int(stream.get("height") or 0)
    rot = 0
    for sd in stream.get("side_data_list") or []:
        rot = int(sd.get("rotation") or 0) or rot
    if abs(rot) in (90, 270):
        w, h = h, w
    if not w or not h:
        raise RuntimeError(f"could not read the video size of {path}")
    return w, h


_CROP = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


def detect_letterbox(path: Path, start: float, duration: float, size: tuple[int, int]) -> Box | None:
    """Black bars baked into the picture (letterbox/pillarbox), or ``None``.

    Samples up to 20 s with ffmpeg's cropdetect and keeps the most common answer, but only when it
    removes a real bar (>= 4% of the height or width), so a dark scene is not mistaken for one.
    """
    w, h = size
    span = max(1.0, min(duration, 20.0))
    cmd = [ff.find_ffmpeg(), "-hide_banner", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{span:.3f}",
           "-i", str(path), "-vf", "fps=2,cropdetect=limit=24:round=2:reset=0", "-an", "-f", "null", "-"]  # fmt: skip
    proc = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW, check=False)
    found = Counter(m.groups() for m in _CROP.finditer(proc.stderr.decode("utf-8", "replace")))
    if not found:
        return None
    cw, ch, cx, cy = (int(v) for v in found.most_common(1)[0][0])
    if cw <= 0 or ch <= 0:
        return None
    if (h - ch) < 0.04 * h and (w - cw) < 0.04 * w:
        return None
    if cw < 0.3 * w or ch < 0.3 * h:  # mostly black: not a letterbox, leave it alone
        return None
    return Box(cw, ch, cx, cy)


def sample_frames(
    path: Path, start: float, duration: float, *, fps: float, width: int, box: Box | None, size: tuple[int, int]
) -> Iterator[tuple[float, np.ndarray, float]]:
    """Yield (time in the clip, BGR frame, scale factor to source pixels) at ``fps``."""
    src_w, src_h = (box.w, box.h) if box else size
    out_w = min(width, src_w) // 2 * 2
    out_h = max(2, round(src_h * out_w / src_w / 2) * 2)
    chain = [box.filter()] if box else []
    chain += [f"fps={fps:g}", f"scale={out_w}:{out_h}"]
    cmd = [ff.find_ffmpeg(), "-hide_banner", "-nostdin", "-v", "error", "-ss", f"{start:.3f}",
           "-t", f"{duration:.3f}", "-i", str(path), "-vf", ",".join(chain), "-an",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]  # fmt: skip
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    frame_bytes = out_w * out_h * 3
    scale = src_w / out_w
    i = 0
    assert proc.stdout is not None
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield i / fps, np.frombuffer(buf, dtype=np.uint8).reshape(out_h, out_w, 3), scale
            i += 1
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()
