"""Cloned voice profiles: reference check, preprocessing and storage (NarraReel NarraReel WHAT_TO_DO.md 5.8).

Zero-shot cloning has no training step: a voice is a cleaned reference clip plus settings, so
profiles are tiny and portable (export/import as a zip). Nothing here needs the cloning model;
it only needs ffmpeg.
"""

from __future__ import annotations

import io
import json
import re
import secrets
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from . import config
from .audio import process as ap
from .errors import ScriptError, SnipmintError

CONSENT_TEXT = (
    "This is my voice, or I have explicit permission from the person whose voice this is. "
    "I won't use it to impersonate or deceive anyone."
)
MIN_SECONDS = 8.0
MAX_SECONDS = 60.0
BEST_WINDOW_SECONDS = 30.0
REFERENCE_SR = 24_000
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

GUIDED_TEXTS = {
    "energetic": (
        "Hey, welcome back! Today I'm going to show you something really exciting. It's quick, "
        "it's simple, and honestly, it changed how I work. Let's jump right in. You're going to love this."
    ),
    "calm": (
        "Take a moment and settle in. What follows is a simple idea, explained slowly and clearly, "
        "so you can follow every step without any rush. There is plenty of time."
    ),
    "hindi": (
        "नमस्ते दोस्तों, आज मैं आपको कुछ बहुत ही मज़ेदार दिखाने वाला हूँ। यह आसान है, तेज़ है, "
        "और सच कहूँ तो इसने मेरे काम करने का तरीका ही बदल दिया। चलिए शुरू करते हैं।"
    ),
}


class VoiceProfileError(SnipmintError):
    """A voice profile is missing, invalid, or the recording can't be used."""


class Check(BaseModel):
    id: str
    label: str
    status: Literal["ok", "warn", "fail"]
    detail: str


class QualityReport(BaseModel):
    duration: float
    checks: list[Check]
    usable: bool = True  # False when any check failed


class Consent(BaseModel):
    accepted: bool
    text: str = CONSENT_TEXT
    at: str


class VoiceProfile(BaseModel):
    id: str
    name: str
    created_at: str
    engine: str = "chatterbox"
    language: str = "en"
    consent: Consent
    reference: str = "reference.wav"
    duration: float = 0.0
    quality: list[Check] = Field(default_factory=list)
    seed: int = 1234


# ------------------------------------------------------------------- quality


def _frame_db(samples: np.ndarray, sr: int, frame_ms: float = 20.0) -> np.ndarray:
    frame = max(1, int(sr * frame_ms / 1000))
    n = samples.size // frame
    if n == 0:
        return np.array([-120.0])
    frames = samples[: n * frame].reshape(n, frame).astype(np.float64)
    rms = np.sqrt(np.mean(frames**2, axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-7))


def check_reference(samples: np.ndarray, sr: int = ap.SAMPLE_RATE) -> QualityReport:
    """Instant, local checks that predict whether a recording will clone well."""
    duration = samples.size / sr
    checks: list[Check] = []

    if duration < MIN_SECONDS:
        checks.append(Check(id="duration", label="Length", status="fail",
                            detail=f"{duration:.0f} s is too short. Record at least {MIN_SECONDS:.0f} seconds."))
    elif duration > MAX_SECONDS:
        checks.append(Check(id="duration", label="Length", status="warn",
                            detail=f"{duration:.0f} s is long. Snipmint will keep the cleanest {BEST_WINDOW_SECONDS:.0f} s."))
    else:
        checks.append(Check(id="duration", label="Length", status="ok", detail=f"{duration:.0f} s"))

    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    clipped = float(np.mean(np.abs(samples) >= 0.944)) if samples.size else 0.0
    if clipped > 0.001:
        checks.append(Check(id="clipping", label="Clipping", status="fail" if clipped > 0.01 else "warn",
                            detail="The recording distorts. Move back from the microphone or lower the input volume."))
    else:
        checks.append(Check(id="clipping", label="Clipping", status="ok",
                            detail=f"Peak {20 * np.log10(max(peak, 1e-7)):.1f} dB"))

    db = _frame_db(samples, sr)
    floor = float(np.percentile(db, 10))
    voiced = db > max(floor + 10.0, -45.0)
    speech_db = float(np.mean(db[voiced])) if voiced.any() else -120.0

    if speech_db < -35.0:
        checks.append(Check(id="level", label="Loudness", status="fail",
                            detail="Too quiet. Speak closer to the microphone or raise the input volume."))
    else:
        checks.append(Check(id="level", label="Loudness", status="ok", detail=f"{speech_db:.0f} dB while speaking"))

    if floor > -50.0:
        checks.append(Check(id="noise", label="Background noise", status="warn",
                            detail="Background noise detected. Try a quieter room; clones copy the noise too."))
    else:
        checks.append(Check(id="noise", label="Background noise", status="ok", detail=f"{floor:.0f} dB between words"))

    ratio = float(np.mean(voiced)) if db.size else 0.0
    if ratio < 0.6:
        checks.append(Check(id="speech", label="Speech", status="warn",
                            detail=f"Only {ratio * 100:.0f}% of the recording is speech. Talk continuously, without long pauses."))
    else:
        checks.append(Check(id="speech", label="Speech", status="ok", detail=f"{ratio * 100:.0f}% speech"))
    return QualityReport(
        duration=duration, checks=checks, usable=not any(c.status == "fail" for c in checks)
    )


# ------------------------------------------------------------- preprocessing


def _collapse_silences(samples: np.ndarray, sr: int, max_gap: float = 0.6, keep: float = 0.3) -> np.ndarray:
    frame = int(sr * 0.02)
    n = samples.size // frame
    if n == 0:
        return samples
    db = _frame_db(samples, sr, 20.0)
    quiet = db < max(float(np.percentile(db, 10)) + 8.0, -50.0)
    out: list[np.ndarray] = []
    i = 0
    while i < n:
        if not quiet[i]:
            out.append(samples[i * frame : (i + 1) * frame])
            i += 1
            continue
        j = i
        while j < n and quiet[j]:
            j += 1
        run = (j - i) * frame
        seg = samples[i * frame : j * frame]
        out.append(seg if run <= max_gap * sr else seg[: int(keep * sr)])
        i = j
    out.append(samples[n * frame :])
    return np.concatenate(out) if out else samples


def _best_window(samples: np.ndarray, sr: int, length: float) -> np.ndarray:
    win = int(length * sr)
    if samples.size <= win:
        return samples
    db = _frame_db(samples, sr, 100.0)
    voiced = (db > max(float(np.percentile(db, 10)) + 10.0, -45.0)).astype(float)
    clip = (np.abs(samples) >= 0.944)
    per_frame = int(sr * 0.1)
    clip_frames = clip[: voiced.size * per_frame].reshape(voiced.size, per_frame).any(axis=1)
    score = voiced - clip_frames * 2.0
    span = int(length * 10)
    sums = np.convolve(score, np.ones(span), mode="valid")
    best = int(np.argmax(sums))
    start = best * per_frame
    return samples[start : start + win]


def preprocess_reference(samples: np.ndarray, sr: int = ap.SAMPLE_RATE) -> np.ndarray:
    """Clean a recording into the reference clip the engine will imitate (section 5.8.2)."""
    x, _ = ap.trim_silence(samples, sr, threshold_db=-48.0, pad_ms=80.0)
    x = ap._pipe_filter(x, sr, "highpass=f=70")
    x = _collapse_silences(x, sr)
    x = _best_window(x, sr, BEST_WINDOW_SECONDS)
    x = ap.loudnorm_two_pass(x, sr, target=-20.0, true_peak=-3.0)
    return ap.resample(x, sr, REFERENCE_SR)


# ------------------------------------------------------------------ storage


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "voice"


def profile_dir(voice_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,80}", voice_id):
        raise VoiceProfileError(f"'{voice_id}' is not a valid voice id.")
    return config.voices_dir() / voice_id


def reference_path(voice_id: str) -> Path:
    return profile_dir(voice_id) / "reference.wav"


def list_profiles() -> list[VoiceProfile]:
    out = []
    for meta in sorted(config.voices_dir().glob("*/profile.json")):
        try:
            out.append(VoiceProfile.model_validate_json(meta.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda p: p.created_at, reverse=True)


def get_profile(voice_id: str) -> VoiceProfile:
    meta = profile_dir(voice_id) / "profile.json"
    try:
        return VoiceProfile.model_validate_json(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VoiceProfileError(
            f"There is no saved voice '{voice_id}'. Clone it again from the My voice tab."
        ) from exc


def decode_recording(data: bytes) -> np.ndarray:
    """Decode an upload (WAV, MP3, M4A, browser WebM/Opus...) by actually decoding it."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ScriptError("That recording is larger than 25 MB.")
    if not data:
        raise ScriptError("The recording is empty.")
    from .errors import FFmpegError

    try:
        samples = ap.decode_to_float32(data, ap.SAMPLE_RATE)
    except FFmpegError as exc:
        raise ScriptError("That file could not be read as audio. Try a WAV, MP3 or M4A recording.") from exc
    if samples.size == 0:
        raise ScriptError("No audio was found in that recording.")
    return samples


def check_recording(data: bytes) -> QualityReport:
    return check_reference(decode_recording(data))


def create_profile(
    data: bytes, name: str, consent: bool, language: str = "en"
) -> tuple[VoiceProfile, QualityReport]:
    """Validate, clean and store a recording as a voice. Consent is mandatory."""
    if not consent:
        raise ScriptError(
            "Confirm that the voice is yours, or that you have permission to clone it."
        )
    name = name.strip()
    if not name:
        raise ScriptError("Give your voice a name.")
    if len(name) > 60:
        raise ScriptError("Voice names can be at most 60 characters.")
    samples = decode_recording(data)
    report = check_reference(samples)
    if not report.usable:
        problems = " ".join(c.detail for c in report.checks if c.status == "fail")
        raise ScriptError(f"This recording can't be used. {problems}")

    reference = preprocess_reference(samples)
    base = _slug(name)
    voice_id, n = base, 2
    while profile_dir(voice_id).exists():
        voice_id, n = f"{base}-{n}", n + 1
    folder = profile_dir(voice_id)
    folder.mkdir(parents=True)
    ap.write_wav(folder / "reference.wav", reference, REFERENCE_SR)
    ap.encode_mp3(folder / "reference.wav", folder / "reference.mp3", "96k")
    profile = VoiceProfile(
        id=voice_id,
        name=name,
        created_at=_now(),
        language=language,
        consent=Consent(accepted=True, at=_now()),
        duration=reference.size / REFERENCE_SR,
        quality=report.checks,
        seed=secrets.randbelow(2**31),
    )
    (folder / "profile.json").write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    return profile, report


def delete_profile(voice_id: str) -> None:
    folder = profile_dir(voice_id)
    if not (folder / "profile.json").is_file():
        raise VoiceProfileError(f"There is no saved voice '{voice_id}'.")
    shutil.rmtree(folder)


def export_profile(voice_id: str) -> bytes:
    folder = profile_dir(voice_id)
    get_profile(voice_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ("profile.json", "reference.wav", "reference.mp3"):
            if (folder / name).is_file():
                zf.write(folder / name, name)
    return buf.getvalue()


def import_profile(data: bytes) -> VoiceProfile:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise VoiceProfileError("That is not a Snipmint voice file (.zip).") from exc
    names = set(zf.namelist())
    if not {"profile.json", "reference.wav"} <= names:
        raise VoiceProfileError("That zip does not contain a Snipmint voice.")
    if any(n not in ("profile.json", "reference.wav", "reference.mp3") for n in names):
        raise VoiceProfileError("That voice file contains unexpected files.")
    try:
        incoming = VoiceProfile.model_validate(json.loads(zf.read("profile.json")))
    except ValueError as exc:
        raise VoiceProfileError("That voice file's profile is damaged.") from exc
    if not incoming.consent.accepted:
        raise VoiceProfileError("That voice has no recorded consent, so it can't be imported.")
    base, voice_id, n = _slug(incoming.name), "", 2
    voice_id = base
    while profile_dir(voice_id).exists():
        voice_id, n = f"{base}-{n}", n + 1
    folder = profile_dir(voice_id)
    folder.mkdir(parents=True)
    for name in names:
        (folder / name).write_bytes(zf.read(name))
    profile = incoming.model_copy(update={"id": voice_id})
    (folder / "profile.json").write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    return profile
