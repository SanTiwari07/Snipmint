"""A deterministic fake voice engine for tests (no network, no models)."""

import threading

import numpy as np

from snipmint.engines.base import SentenceAudio, TTSEngine
from snipmint.models import EngineCapabilities, EngineStatus

SR = 48000
SECONDS_PER_CHAR = 0.06


class FakeEngine(TTSEngine):
    id = "fake"
    display_name = "Fake"
    capabilities = EngineCapabilities(native_speed=True, native_word_timing=True)

    def __init__(self, native_words=True, native_speed=True):
        self.calls = 0
        self.native_words = native_words
        self.capabilities = EngineCapabilities(
            native_speed=native_speed, native_word_timing=native_words, energy_control="none"
        )
        self.lock = threading.Lock()

    def status(self):
        return EngineStatus(available=True)

    def list_voices(self, language=None):
        return []

    def synthesize(self, text, voice):
        with self.lock:
            self.calls += 1
        speed = voice.speed if self.capabilities.native_speed else 1.0
        words = text.split()
        total_chars = sum(len(w) for w in words)
        seconds = total_chars * SECONDS_PER_CHAR / speed
        lead = 0.12  # leading silence the pipeline must trim and account for
        n = int((seconds + lead + 0.2) * SR)
        t = np.arange(n) / SR
        sig = np.zeros(n, dtype=np.float32)
        body = (t >= lead) & (t < lead + seconds)
        sig[body] = 0.3 * np.sin(2 * np.pi * 180 * t[body])
        timed = None
        if self.native_words:
            timed, cursor = [], lead
            for w in words:
                dur = len(w) * SECONDS_PER_CHAR / speed
                timed.append((w.strip(".,!?"), cursor, cursor + dur))
                cursor += dur
        return SentenceAudio(sig, SR, timed)


