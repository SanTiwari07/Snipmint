"""Audio helpers: decode, trim, assemble, tempo, energy, loudness, write (section 5.6)."""

from __future__ import annotations

import json
import math
import os
import re
import wave
from pathlib import Path

import numpy as np

from ..errors import FFmpegError
from .ffmpeg import run_ffmpeg

SAMPLE_RATE = 48_000


# ----------------------------------------------------------------- decode


def decode_to_float32(source: bytes | str | os.PathLike[str], sr: int = SAMPLE_RATE) -> np.ndarray:
    """Decode any audio ffmpeg understands to mono float32 at ``sr``."""
    out = ["-v", "error", "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"]
    if isinstance(source, bytes | bytearray):
        proc = run_ffmpeg(["-i", "pipe:0", *out], input=bytes(source))
    else:
        proc = run_ffmpeg(["-i", str(source), *out])
    return np.frombuffer(proc.stdout, dtype="<f4").copy()


def _pipe_filter(samples: np.ndarray, sr: int, af: str, out_sr: int | None = None) -> np.ndarray:
    """Push float32 mono samples through an ffmpeg audio filter and back."""
    out_sr = out_sr or sr
    data = np.ascontiguousarray(samples, dtype="<f4").tobytes()
    proc = run_ffmpeg(
        [
            "-v", "error",
            "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0",
            "-af", af,
            "-ar", str(out_sr), "-ac", "1", "-f", "f32le", "pipe:1",
        ],
        input=data,
    )  # fmt: skip
    return np.frombuffer(proc.stdout, dtype="<f4").copy()


def resample(samples: np.ndarray, sr_from: int, sr_to: int = SAMPLE_RATE) -> np.ndarray:
    if sr_from == sr_to or samples.size == 0:
        return np.asarray(samples, dtype=np.float32)
    return _pipe_filter(samples, sr_from, "anull", out_sr=sr_to)


# ------------------------------------------------------------------- silence


def make_silence(seconds: float, sr: int = SAMPLE_RATE) -> np.ndarray:
    return np.zeros(max(0, round(seconds * sr)), dtype=np.float32)


def trim_silence(
    samples: np.ndarray,
    sr: int = SAMPLE_RATE,
    threshold_db: float = -45.0,
    pad_ms: float = 30.0,
) -> tuple[np.ndarray, float]:
    """Trim leading/trailing silence, keeping ``pad_ms``.

    Returns (clip, seconds trimmed from the start).
    """
    if samples.size == 0:
        return samples, 0.0
    frame = max(1, int(sr * 0.010))
    n = samples.size // frame
    if n == 0:
        return samples, 0.0
    frames = samples[: n * frame].reshape(n, frame)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    thresh = 10 ** (threshold_db / 20)
    loud = np.nonzero(rms > thresh)[0]
    if loud.size == 0:
        return samples[: int(sr * 0.05)], 0.0  # all silence: keep a sliver
    pad = int(sr * pad_ms / 1000)
    start = max(0, int(loud[0]) * frame - pad)
    end = min(samples.size, (int(loud[-1]) + 1) * frame + pad)
    return samples[start:end], start / sr


# ----------------------------------------------------------------- assemble


def assemble(
    clips: list[np.ndarray],
    pauses: list[float],
    lead: float,
    tail: float,
    sr: int = SAMPLE_RATE,
) -> tuple[np.ndarray, list[tuple[float, float]]]:
    """Join clips with exact sample positions. ``pauses[i]`` follows clip ``i``.

    Returns the audio and each clip's (start, end) in seconds.
    """
    if len(pauses) != max(0, len(clips) - 1):
        raise ValueError("pauses must have exactly len(clips) - 1 entries")
    lead_n = round(lead * sr)
    pause_n = [round(p * sr) for p in pauses]
    tail_n = round(tail * sr)
    total = lead_n + sum(c.size for c in clips) + sum(pause_n) + tail_n
    out = np.zeros(total, dtype=np.float32)
    spans: list[tuple[float, float]] = []
    pos = lead_n
    for i, clip in enumerate(clips):
        out[pos : pos + clip.size] = clip
        spans.append((pos / sr, (pos + clip.size) / sr))
        pos += clip.size
        if i < len(pause_n):
            pos += pause_n[i]
    return out, spans


# ------------------------------------------------------------------- tempo


def tempo_filter(factor: float) -> str:
    """``atempo`` chain for any factor (each stage must stay within 0.5-2.0)."""
    if factor <= 0:
        raise ValueError("tempo factor must be positive")
    stages: list[float] = []
    f = factor
    while f > 2.0:
        stages.append(2.0)
        f /= 2.0
    while f < 0.5:
        stages.append(0.5)
        f /= 0.5
    stages.append(f)
    return ",".join(f"atempo={s:.6f}" for s in stages)


def change_tempo(samples: np.ndarray, factor: float, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Speed up (>1) or slow down (<1) without changing pitch."""
    if abs(factor - 1.0) < 1e-4 or samples.size == 0:
        return samples
    return _pipe_filter(samples, sr, tempo_filter(factor))


# ------------------------------------------------------------------ energy


def energy_filter(energy: float) -> str:
    """ffmpeg filter chain for the 'energy' post-processing (section 5.4).

    ``energy`` is 0.0 (calm) .. 1.0 (hype); 0.5 is a neutral, gentle chain.
    """
    e = min(1.0, max(0.0, energy))
    ratio = 1.5 + 2.5 * e  # 1.5:1 .. 4:1
    threshold_db = -16 - 6 * e
    eq_gain = -1.0 + 4.0 * e  # -1 dB .. +3 dB around the presence band
    return (
        "highpass=f=80,"
        f"acompressor=threshold={threshold_db:.1f}dB:ratio={ratio:.2f}:attack=5:release=60,"
        f"equalizer=f=4000:t=q:w=1.2:g={eq_gain:.2f}"
    )


def apply_energy(samples: np.ndarray, energy: float, sr: int = SAMPLE_RATE) -> np.ndarray:
    if samples.size == 0:
        return samples
    return _pipe_filter(samples, sr, energy_filter(energy))


# ---------------------------------------------------------------- loudness

_JSON_BLOCK = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)


def measure_loudness(samples: np.ndarray, sr: int = SAMPLE_RATE, target: float = -14.0) -> dict:
    """First loudnorm pass: returns ffmpeg's measured values (strings)."""
    data = np.ascontiguousarray(samples, dtype="<f4").tobytes()
    proc = run_ffmpeg(
        [
            "-v", "info",
            "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0",
            "-af", f"loudnorm=I={target}:TP=-1.0:LRA=11:print_format=json",
            "-f", "null", "-",
        ],
        input=data,
    )  # fmt: skip
    m = _JSON_BLOCK.search(proc.stderr.decode("utf-8", "replace"))
    if not m:
        raise FFmpegError("Could not measure loudness (ffmpeg printed no loudnorm result).")
    return json.loads(m.group())


def loudnorm_two_pass(
    samples: np.ndarray, sr: int = SAMPLE_RATE, target: float = -14.0, true_peak: float = -1.0
) -> np.ndarray:
    """Two-pass EBU R128 loudness normalisation to ``target`` LUFS."""
    if samples.size == 0 or not np.any(np.abs(samples) > 1e-5):
        return samples
    stats = measure_loudness(samples, sr, target)
    try:
        measured_i = float(stats["input_i"])
    except (KeyError, ValueError):
        return samples
    if not math.isfinite(measured_i):
        return samples
    af = (
        f"loudnorm=I={target}:TP={true_peak}:LRA=11:"
        f"measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
        f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:"
        f"offset={stats['target_offset']}:linear=true"
    )
    # loudnorm resamples internally to 192 kHz; bring it back to the working rate.
    return _pipe_filter(samples, sr, f"{af},aresample={sr}")


def integrated_loudness(samples: np.ndarray, sr: int = SAMPLE_RATE) -> float:
    """Measured integrated loudness in LUFS (for checks and tests)."""
    return float(measure_loudness(samples, sr)["input_i"])


# -------------------------------------------------------------------- output


def write_wav(path: str | os.PathLike[str], samples: np.ndarray, sr: int = SAMPLE_RATE) -> None:
    """16-bit PCM mono WAV via the standard library."""
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def read_wav(path: str | os.PathLike[str]) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        raw = wf.readframes(wf.getnframes())
    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1)
    return pcm, sr


def encode_mp3(
    wav: str | os.PathLike[str], mp3: str | os.PathLike[str], bitrate: str = "192k"
) -> Path:
    run_ffmpeg(["-v", "error", "-i", str(wav), "-c:a", "libmp3lame", "-b:a", bitrate, str(mp3)])
    return Path(mp3)
