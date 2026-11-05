"""Engine interface. Engines synthesize ONE sentence at a time (NarraReel NarraReel WHAT_TO_DO.md section 5.3.1)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from ..models import EngineCapabilities, EngineStatus, VoiceInfo, VoiceSelection


@dataclass
class SentenceAudio:
    """Result of one synthesis call."""

    samples: np.ndarray  # float32 mono
    sample_rate: int
    # (word, start, end) in seconds relative to the clip, when the engine reports them
    words: list[tuple[str, float, float]] | None = None


ProgressFn = Callable[[float, str], None]  # (fraction 0..1, message)


class TTSEngine(ABC):
    id: str
    display_name: str
    capabilities: EngineCapabilities
    #: bump when output changes, so cached sentences are invalidated
    version: str = "1"
    #: how many sentences the pipeline may synthesize at the same time
    max_parallel: int = 1

    @abstractmethod
    def status(self) -> EngineStatus: ...

    @abstractmethod
    def list_voices(self, language: str | None = None) -> list[VoiceInfo]: ...

    def prepare(self, progress: ProgressFn | None = None) -> None:  # noqa: B027
        """Download / load models. Default: nothing to do."""

    @abstractmethod
    def synthesize(self, text: str, voice: VoiceSelection) -> SentenceAudio: ...

    def cache_voice_key(self, voice: VoiceSelection) -> str:
        """What identifies this voice's sound in the sentence cache. Engines whose voice can change
        without its id changing (a re-recorded clone, a different model variant) override this."""
        return voice.voice_id

    def get_voice(self, voice_id: str) -> VoiceInfo | None:
        for v in self.list_voices():
            if v.id == voice_id:
                return v
        return None
