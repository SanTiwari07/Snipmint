"""Non-AI signals about a source: loudness over time, camera cuts and the YouTube heatmap."""

from __future__ import annotations

import logging
import threading
import wave
from pathlib import Path

import numpy as np
from pydantic import BaseModel, Field

from ..audio import ffmpeg as ff
from ..errors import JobCancelled
from ..ingest.source import SourceMeta

log = logging.getLogger("snipmint.signals")

HOP = 0.5  # seconds per loudness sample


class Signals(BaseModel):
    hop: float = HOP
    loudness_db: list[float] = Field(default_factory=list)  # one value per hop
    scene_cuts: list[float] = Field(default_factory=list)  # seconds
    heatmap: list[float] = Field(default_factory=list)  # one value per second, 0..1, may be empty

    # ------------------------------------------------------------------ queries

    def loudness_z(self) -> np.ndarray:
        arr = np.asarray(self.loudness_db, dtype=np.float32)
        if arr.size == 0:
            return arr
        voiced = arr[arr > -55]  # ignore silence when computing the baseline
        base = voiced if voiced.size > 10 else arr
        std = float(base.std()) or 1.0
        return (arr - float(base.mean())) / std

    def mean_heat(self, start: float, end: float) -> float | None:
        if not self.heatmap:
            return None
        a, b = int(start), max(int(start) + 1, int(end))
        seg = self.heatmap[a:b]
        return float(np.mean(seg)) if seg else None

    def cuts_between(self, start: float, end: float) -> int:
        return sum(1 for c in self.scene_cuts if start < c < end)


def extract_audio(video: Path, dest: Path) -> Path:
    """16 kHz mono WAV: what Whisper wants, and cheap to scan for loudness."""
    ff.run_ffmpeg(["-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                   "-c:a", "pcm_s16le", str(dest)])  # fmt: skip
    return dest


def loudness_curve(audio_wav: Path, hop: float = HOP) -> list[float]:
    """RMS level in dBFS for each ``hop`` seconds, read in chunks (hour-long files are fine)."""
    out: list[float] = []
    with wave.open(str(audio_wav), "rb") as wf:
        sr, ch = wf.getframerate(), wf.getnchannels()
        frames = max(1, int(sr * hop))
        while True:
            raw = wf.readframes(frames)
            if not raw:
                break
            pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            if ch > 1:
                pcm = pcm.reshape(-1, ch).mean(axis=1)
            rms = float(np.sqrt(np.mean(pcm * pcm))) if pcm.size else 0.0
            out.append(round(20 * np.log10(max(rms, 1e-5)), 2))
    return out


def heatmap_curve(meta: SourceMeta) -> list[float]:
    """YouTube "most replayed" values, one per second, scaled to 0..1 (empty if not available)."""
    if not meta.heatmap or not meta.duration:
        return []
    seconds = int(meta.duration) + 1
    curve = np.zeros(seconds, dtype=np.float32)
    for p in meta.heatmap:
        a, b = max(0, int(p.start)), min(seconds, max(int(p.start) + 1, int(np.ceil(p.end))))
        curve[a:b] = np.maximum(curve[a:b], p.value)
    top = float(curve.max()) or 1.0
    return [round(float(v) / top, 4) for v in curve]


def scene_cuts(video: Path, cancel: threading.Event | None = None) -> list[float]:
    """Camera cut times with PySceneDetect (optional extra; returns [] without it)."""
    try:
        from scenedetect import ContentDetector, SceneManager, open_video
    except ImportError:
        log.info("PySceneDetect not installed; skipping camera-cut detection")
        return []
    try:
        video_stream = open_video(str(video))
        manager = SceneManager()
        manager.auto_downscale = True
        manager.add_detector(ContentDetector(threshold=27.0))

        def stop_if_cancelled(*_args) -> None:
            if cancel is not None and cancel.is_set():
                raise JobCancelled("Cancelled.")

        manager.detect_scenes(video_stream, callback=stop_if_cancelled, show_progress=False, frame_skip=1)
        scenes = manager.get_scene_list()
    except JobCancelled:
        raise
    except Exception as exc:  # a broken decoder must not fail the whole job
        log.warning("Scene detection failed: %s", exc)
        return []
    return [round(s[0].get_seconds(), 3) for s in scenes[1:]]


def compute(video: Path, audio_wav: Path, meta: SourceMeta,
            cancel: threading.Event | None = None) -> Signals:  # fmt: skip
    return Signals(
        loudness_db=loudness_curve(audio_wav),
        scene_cuts=scene_cuts(video, cancel),
        heatmap=heatmap_curve(meta),
    )
