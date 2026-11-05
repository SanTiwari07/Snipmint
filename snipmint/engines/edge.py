"""Microsoft Edge online voices via the ``edge-tts`` package (NarraReel NarraReel WHAT_TO_DO.md section 5.3.2).

Unofficial, online service. Word boundaries are native (100-nanosecond ticks).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from typing import Any

from .. import config
from ..audio.process import SAMPLE_RATE, decode_to_float32
from ..errors import EngineError
from ..models import EngineCapabilities, EngineStatus, VoiceInfo, VoiceSelection
from .base import SentenceAudio, TTSEngine

log = logging.getLogger("snipmint.engines.edge")

TICKS_PER_SECOND = 10_000_000  # edge-tts reports offsets/durations in 100 ns units
VOICE_CACHE_SECONDS = 24 * 3600
RETRY_DELAYS = (1.0, 2.0, 4.0)
MAX_PARALLEL = 4

# (id, name, accent, gender) - shown first in every list.
CURATED: list[tuple[str, str, str, str]] = [
    ("en-US-AvaNeural", "Ava", "US", "female"),
    ("en-US-AndrewNeural", "Andrew", "US", "male"),
    ("en-US-JennyNeural", "Jenny", "US", "female"),
    ("en-US-GuyNeural", "Guy", "US", "male"),
    ("en-US-AriaNeural", "Aria", "US", "female"),
    ("en-US-ChristopherNeural", "Christopher", "US", "male"),
    ("en-US-EricNeural", "Eric", "US", "male"),
    ("en-US-BrianNeural", "Brian", "US", "male"),
    ("en-US-MichelleNeural", "Michelle", "US", "female"),
    ("en-US-AnaNeural", "Ana", "US", "female"),
    ("en-US-SteffanNeural", "Steffan", "US", "male"),
    ("en-GB-RyanNeural", "Ryan", "UK", "male"),
    ("en-GB-SoniaNeural", "Sonia", "UK", "female"),
    ("en-IN-PrabhatNeural", "Prabhat", "IN", "male"),
    ("en-IN-NeerjaNeural", "Neerja", "IN", "female"),
    ("hi-IN-MadhurNeural", "Madhur", "IN", "male"),
    ("hi-IN-SwaraNeural", "Swara", "IN", "female"),
]

_ACCENT_NAMES = {"GB": "UK"}


class _AsyncRunner:
    """A private event loop in a worker thread so callers stay synchronous."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="snipmint-edge-loop", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()


def energy_to_prosody(energy: float, speed: float) -> tuple[str, str, str]:
    """(rate, pitch, volume) strings for edge-tts. Small, safe ranges (section 5.3.2)."""
    e = min(1.0, max(0.0, energy))
    rate_pct = round((speed - 1.0) * 100 + (-3 + 7 * e))
    pitch_hz = round(-4 + 10 * e)
    volume_pct = round(-5 + 10 * e)
    return f"{rate_pct:+d}%", f"{pitch_hz:+d}Hz", f"{volume_pct:+d}%"


def _voice_info(raw: dict) -> VoiceInfo:
    short = raw["ShortName"]
    locale = raw.get("Locale", short[:5])
    parts = short.split("-")
    name = parts[2].replace("Neural", "") if len(parts) > 2 else short
    region = locale.split("-")[-1]
    gender = str(raw.get("Gender", "")).lower()
    return VoiceInfo(
        engine="edge",
        id=short,
        name=name,
        language=locale,
        gender=gender if gender in ("female", "male") else "unknown",
        accent=_ACCENT_NAMES.get(region, region),
        offline=False,
    )


class EdgeEngine(TTSEngine):
    id = "edge"
    display_name = "Online voices (Microsoft Edge)"
    version = "1"
    max_parallel = MAX_PARALLEL
    capabilities = EngineCapabilities(
        offline=False,
        native_word_timing=True,
        native_speed=True,
        energy_control="basic",
        cloning=False,
        languages=["en", "hi", "multi"],
        needs_gpu="no",
    )

    def __init__(self) -> None:
        self._runner: _AsyncRunner | None = None
        self._runner_lock = threading.Lock()
        self._sem = threading.Semaphore(MAX_PARALLEL)
        self._voices: list[VoiceInfo] | None = None

    # -------------------------------------------------------------- status

    def status(self) -> EngineStatus:
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            return EngineStatus(available=False, reason='Install with: pip install "snipmint"')
        return EngineStatus(available=True)

    # -------------------------------------------------------------- voices

    def _curated(self) -> list[VoiceInfo]:
        return [
            VoiceInfo(
                engine="edge",
                id=vid,
                name=name,
                language=vid[:5],
                gender=gender,  # type: ignore[arg-type]
                accent=accent,
            )
            for vid, name, accent, gender in CURATED
        ]

    def _cache_file(self):
        return config.cache_dir() / "edge_voices.json"

    def _load_cached(self) -> list[dict] | None:
        path = self._cache_file()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - float(data["fetched"]) < VOICE_CACHE_SECONDS:
                return data["voices"]
        except (OSError, ValueError, KeyError):
            pass
        return None

    def _fetch_all(self) -> list[dict]:
        import edge_tts

        cached = self._load_cached()
        if cached is not None:
            return cached
        try:
            raw = self._get_runner().run(edge_tts.list_voices())
        except Exception as exc:
            log.warning("Could not fetch the Edge voice list: %s", exc)
            return []
        slim = [
            {k: v.get(k) for k in ("ShortName", "Gender", "Locale")}
            for v in raw
            if v.get("ShortName")
        ]
        with contextlib.suppress(OSError):
            self._cache_file().write_text(
                json.dumps({"fetched": time.time(), "voices": slim}), encoding="utf-8"
            )
        return slim

    def list_voices(self, language: str | None = None) -> list[VoiceInfo]:
        if self._voices is None:
            curated = self._curated()
            seen = {v.id for v in curated}
            extra = [_voice_info(r) for r in self._fetch_all() if r["ShortName"] not in seen]
            extra.sort(key=lambda v: (v.language, v.name))
            self._voices = curated + extra
        voices = self._voices
        if language:
            prefix = language.lower()
            voices = [v for v in voices if v.language.lower().startswith(prefix)]
        return list(voices)

    def get_voice(self, voice_id: str) -> VoiceInfo | None:
        for v in self._curated():
            if v.id == voice_id:
                return v
        return super().get_voice(voice_id)

    # ----------------------------------------------------------- synthesis

    def _get_runner(self) -> _AsyncRunner:
        with self._runner_lock:
            if self._runner is None:
                self._runner = _AsyncRunner()
            return self._runner

    async def _stream(
        self, text: str, voice_id: str, rate: str, pitch: str, volume: str
    ) -> tuple[bytes, list[tuple[str, float, float]]]:
        import edge_tts

        comm = edge_tts.Communicate(
            text, voice_id, rate=rate, pitch=pitch, volume=volume, boundary="WordBoundary"
        )
        audio = bytearray()
        words: list[tuple[str, float, float]] = []
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / TICKS_PER_SECOND
                end = start + chunk["duration"] / TICKS_PER_SECOND
                words.append((chunk["text"], start, end))
        return bytes(audio), words

    def synthesize(self, text: str, voice: VoiceSelection) -> SentenceAudio:
        rate, pitch, volume = energy_to_prosody(voice.energy, voice.speed)
        last_error: Exception | None = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                with self._sem:
                    audio, words = self._get_runner().run(
                        self._stream(text, voice.voice_id, rate, pitch, volume)
                    )
                if not audio:
                    raise EngineError("The voice service returned no audio.")
                samples = decode_to_float32(audio, SAMPLE_RATE)
                return SentenceAudio(samples, SAMPLE_RATE, words or None)
            except ValueError as exc:  # bad voice id / prosody value: retrying will not help
                raise EngineError(f"Voice '{voice.voice_id}' was rejected: {exc}") from exc
            except Exception as exc:  # network errors, NoAudioReceived, timeouts
                last_error = exc
                if attempt < len(RETRY_DELAYS):
                    log.info("Edge synthesis failed (%s); retrying in %.0fs", exc, RETRY_DELAYS[attempt])
                    time.sleep(RETRY_DELAYS[attempt])
        raise EngineError(
            "Couldn't get speech from Microsoft's voice service "
            f"({type(last_error).__name__}: {last_error}). Check your internet connection, "
            "or switch to an offline voice (pip install \"snipmint[offline]\")."
        ) from last_error
