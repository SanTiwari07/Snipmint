"""Prepare a source: probe -> download -> audio -> transcribe -> signals -> analyze.

Every stage writes its result into the source folder and is skipped when that result already
exists, so a restart (or a second analysis with other settings) resumes instead of redoing work.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable

from ..analyze import service, signals
from ..analyze.schemas import AnalyzeOptions
from ..errors import IngestError, JobCancelled, SnipmintError
from ..ingest import youtube
from ..ingest.source import Source
from ..models import ProgressEvent
from ..transcribe import whisper

log = logging.getLogger("snipmint.source")

ProgressCallback = Callable[[ProgressEvent], None]

# (stage, start fraction, end fraction) of the overall progress bar
PLAN = [
    ("probe", 0.00, 0.03),
    ("download", 0.03, 0.40),
    ("audio", 0.40, 0.45),
    ("transcribe", 0.45, 0.85),
    ("signals", 0.85, 0.93),
    ("analyze", 0.93, 1.00),
]
_SPAN = {name: (a, b) for name, a, b in PLAN}


def max_source_minutes() -> float:
    try:
        return float(os.environ.get("MAX_SOURCE_MINUTES") or 180)
    except ValueError:
        return 180.0


def _emit(progress: ProgressCallback | None, stage: str, frac: float, message: str) -> None:
    if progress is None:
        return
    a, b = _SPAN[stage]
    progress(ProgressEvent(stage=stage, fraction=round(a + (b - a) * max(0.0, min(1.0, frac)), 4),  # type: ignore[arg-type]
                           message=message))  # fmt: skip


def _check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise JobCancelled("Cancelled.")


def prepare_source(
    source: Source,
    url: str,
    opts: AnalyzeOptions,
    *,
    gemini_key: str | None = None,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> dict:
    with source.lock:
        source.update_status(lambda st: setattr(st, "error", None))
        current = "probe"
        try:
            # ---- probe
            current = "probe"
            meta = source.meta
            if meta is None:
                source.set_stage("probe", "running")
                _emit(progress, "probe", 0.1, "Reading video details")
                meta = youtube.probe(url)
                limit = max_source_minutes()
                if meta.duration > limit * 60:
                    raise IngestError(
                        f"This video is {meta.duration / 60:.0f} minutes long; the limit is "
                        f"{limit:.0f} minutes (MAX_SOURCE_MINUTES)."
                    )
                source.save_meta(meta)
            source.set_stage("probe", "done")
            _check(cancel)

            # ---- download
            current = "download"
            if not source.video.is_file():
                source.set_stage("download", "running")
                youtube.download(
                    url, source.video, cancel=cancel,
                    progress=lambda f, m: _emit(progress, "download", f, m),
                )  # fmt: skip
            source.set_stage("download", "done")
            _check(cancel)

            # ---- audio
            current = "audio"
            if not source.audio.is_file():
                source.set_stage("audio", "running")
                _emit(progress, "audio", 0.2, "Extracting audio")
                tmp = source.file("audio.tmp.wav")
                signals.extract_audio(source.video, tmp)
                tmp.replace(source.audio)
            source.set_stage("audio", "done")
            _check(cancel)

            # ---- transcribe
            current = "transcribe"
            if not source.file("transcript.json").is_file():
                source.set_stage("transcribe", "running")
                transcript = whisper.transcribe(
                    source.audio, duration=meta.duration, cancel=cancel,
                    progress=lambda f, m: _emit(progress, "transcribe", f, m),
                )  # fmt: skip
                source.write_json("transcript.json", transcript.model_dump())
            source.set_stage("transcribe", "done")
            _check(cancel)

            # ---- signals
            current = "signals"
            if not source.file("signals.json").is_file():
                source.set_stage("signals", "running")
                _emit(progress, "signals", 0.1, "Measuring loudness and camera cuts")
                sig = signals.compute(source.video, source.audio, meta, cancel)
                source.write_json("signals.json", sig.model_dump())
            source.set_stage("signals", "done")
            _check(cancel)

            # ---- analyze
            current = "analyze"
            source.set_stage("analyze", "running")
            result = service.analyze(
                source, opts, gemini_key=gemini_key,
                progress=lambda f, m: _emit(progress, "analyze", f, m),
            )  # fmt: skip
            source.set_stage("analyze", "done")
            warnings = [result.fallback_reason] if result.fallback_reason else []
            source.update_status(lambda st: setattr(st, "warnings", warnings))
            _emit(progress, "analyze", 1.0, f"Found {len(result.moments)} moments")
            return {"source_id": source.id, "provider": result.provider,
                    "moments": len(result.moments), "fallback_reason": result.fallback_reason}  # fmt: skip
        except JobCancelled:
            source.set_stage(current, "pending")
            raise
        except SnipmintError as exc:
            source.set_stage(current, "failed", str(exc))
            raise
        except Exception as exc:
            log.exception("Source %s failed at %s", source.id, current)
            source.set_stage(current, "failed", f"Unexpected error: {exc}")
            raise


def reanalyze(
    source: Source,
    opts: AnalyzeOptions,
    *,
    gemini_key: str | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    """Run only the analysis again (other length, count or provider)."""
    with source.lock:
        source.set_stage("analyze", "running")
        try:
            result = service.analyze(
                source, opts, gemini_key=gemini_key,
                progress=lambda f, m: progress and progress(
                    ProgressEvent(stage="analyze", fraction=f, message=m)),
            )  # fmt: skip
        except SnipmintError as exc:
            source.set_stage("analyze", "failed", str(exc))
            raise
        source.set_stage("analyze", "done")
        warnings = [result.fallback_reason] if result.fallback_reason else []
        source.update_status(lambda st: setattr(st, "warnings", warnings))
        return {"source_id": source.id, "provider": result.provider, "moments": len(result.moments),
                "fallback_reason": result.fallback_reason}  # fmt: skip
