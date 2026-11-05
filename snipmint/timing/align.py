"""Optional forced alignment with faster-whisper (``pip install "snipmint[align]"``).

Runs on each short sentence clip, returns recognised words with times; they are then mapped
onto script tokens with :func:`snipmint.timing.native.map_words_to_tokens`.
"""

from __future__ import annotations

import logging
import os
import threading

import numpy as np

from .. import config
from ..audio.process import resample
from ..errors import DependencyMissing
from .native import EngineWord

log = logging.getLogger("snipmint.timing.align")

INSTALL_HINT = 'Install with: pip install "snipmint[align]"'
_model = None
_lock = threading.Lock()


def is_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return False
    return True


def _load_model():
    global _model
    with _lock:
        if _model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise DependencyMissing("faster-whisper", INSTALL_HINT) from exc
            name = os.environ.get("SNIPMINT_WHISPER_MODEL", "base")
            root = config.models_dir() / "whisper"
            root.mkdir(parents=True, exist_ok=True)
            log.info("Loading faster-whisper model '%s' (downloads on first use)", name)
            _model = WhisperModel(name, device="cpu", compute_type="int8", download_root=str(root))
        return _model


def align_clip(samples: np.ndarray, sr: int, language: str | None = None) -> list[EngineWord]:
    """Recognise the words of one sentence clip with timestamps (seconds, clip-relative)."""
    model = _load_model()
    audio = resample(samples, sr, 16_000).astype(np.float32)
    with _lock:  # CTranslate2 models are not meant to be shared across threads
        segments, _ = model.transcribe(
            audio,
            language=language,
            word_timestamps=True,
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        words: list[EngineWord] = []
        for seg in segments:
            for w in seg.words or []:
                words.append((w.word.strip(), float(w.start), float(w.end)))
    return words
