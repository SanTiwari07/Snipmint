"""Transcribe the source audio with faster-whisper (word timestamps).

Settings follow openshorts/subtitles.py ``get_whisper_config`` and the continuation-word merge
is ported from openshorts/subtitles.py ``merge_continuation_words`` (MIT, (c) 2024 OpenShorts).
"""

from __future__ import annotations

import gc
import logging
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path

from .. import config
from ..errors import AnalysisError, DependencyMissing, JobCancelled
from .models import Segment, Transcript, Word

log = logging.getLogger("snipmint.transcribe")

INSTALL_HINT = 'Install with: pip install "snipmint[transcribe]"'
_lock = threading.Lock()


def is_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return False
    return True


def whisper_config() -> tuple[str, str, str]:
    """(model, device, compute_type) from the environment, with CPU-friendly defaults."""
    model = (os.environ.get("WHISPER_MODEL") or "small").strip()
    device = (os.environ.get("WHISPER_DEVICE") or "cpu").strip()
    compute = (os.environ.get("WHISPER_COMPUTE") or ("float16" if device == "cuda" else "int8")).strip()
    return model, device, compute


_CONTINUATION = re.compile(r"^[\-'’](?:[a-z]{1,3})?$|^n['’]t$|^['’](?:s|t|re|ve|ll|d|m)$", re.I)


def merge_continuation_words(words: list[Word]) -> list[Word]:
    """Glue tokens Whisper splits off a word ("don" + "'t", "e" + "-mail") back onto it."""
    out: list[Word] = []
    for w in words:
        text = w.text.strip()
        if not text:
            continue
        if out and not w.text.startswith(" ") and (_CONTINUATION.match(text) or text.startswith(("'", "’"))):
            prev = out[-1]
            out[-1] = Word(text=prev.text + text, start=prev.start, end=w.end, prob=min(prev.prob, w.prob))
            continue
        out.append(Word(text=text, start=w.start, end=w.end, prob=w.prob))
    return out


def _read_pcm16k(path: Path):
    from ..audio.process import read_wav, resample

    pcm, sr = read_wav(path)
    if sr != 16_000:
        pcm = resample(pcm, sr, 16_000)
    return pcm.astype("float32", copy=False)


def transcribe(
    audio: Path,
    *,
    duration: float = 0.0,
    language: str | None = None,
    progress: Callable[[float, str], None] | None = None,
    cancel: threading.Event | None = None,
) -> Transcript:
    """Words with start/end times for the whole file. Frees the model afterwards."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise DependencyMissing("faster-whisper", INSTALL_HINT) from exc

    name, device, compute = whisper_config()
    root = config.models_dir() / "whisper"
    root.mkdir(parents=True, exist_ok=True)
    if progress:
        progress(0.0, f"Loading speech model ({name}); the first run downloads it")
    with _lock:
        try:
            model = WhisperModel(name, device=device, compute_type=compute, download_root=str(root))
        except Exception as exc:  # CUDA missing, bad model name...
            if device != "cpu":
                log.warning("Whisper on %s failed (%s); falling back to CPU", device, exc)
                model = WhisperModel(name, device="cpu", compute_type="int8", download_root=str(root))
            else:
                raise AnalysisError(f"Could not load the speech model '{name}': {exc}") from exc
        try:
            # Hand Whisper the samples ourselves: faster-whisper's own decoder (PyAV) breaks
            # between versions, and audio.wav is already 16 kHz mono PCM.
            samples = _read_pcm16k(audio)
            segments_iter, info = model.transcribe(
                samples,
                language=language,
                word_timestamps=True,
                vad_filter=True,
                beam_size=5,
                condition_on_previous_text=False,
            )
            total = duration or float(getattr(info, "duration", 0.0) or 0.0)
            segments: list[Segment] = []
            for seg in segments_iter:
                if cancel is not None and cancel.is_set():
                    raise JobCancelled("Cancelled.")
                words = merge_continuation_words([
                    Word(text=w.word, start=float(w.start), end=float(w.end),
                         prob=float(getattr(w, "probability", 1.0) or 1.0))
                    for w in (seg.words or [])
                ])  # fmt: skip
                segments.append(Segment(start=float(seg.start), end=float(seg.end),
                                        text=seg.text.strip(), words=words))  # fmt: skip
                if progress and total > 0:
                    frac = min(0.99, float(seg.end) / total)
                    progress(frac, f"Transcribing {int(frac * 100)}%")
            lang = str(getattr(info, "language", None) or language or "unknown")
        finally:
            del model
            gc.collect()
    return Transcript(language=lang, duration=total, segments=segments)
