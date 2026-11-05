"""Script + voice + plan -> narration audio with exact word timings (NarraReel NarraReel WHAT_TO_DO.md 5.6)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

from .. import config
from ..audio import process as ap
from ..captions.srt import build_srt
from ..engines.base import SentenceAudio, TTSEngine
from ..engines.registry import resolve_voice
from ..errors import EngineError, EngineUnavailable, JobCancelled, ScriptError
from ..jobs.workspace import Workspace
from ..models import (
    ProgressEvent,
    ScriptAnalysis,
    SynthesisResult,
    VoiceSelection,
    WordTiming,
)
from ..script import duration as dur
from ..script.analyze import SynthUnit, analyze_script, synthesis_units
from ..timing import align as aligner
from ..timing.estimate import estimate_timings
from ..timing.native import native_timings
from ..timing.quality import enforce_quality
from .cache import CachedClip, SentenceCache, make_key

log = logging.getLogger("snipmint.speak")

SR = ap.SAMPLE_RATE
MAX_TEMPO_CORRECTION = 0.06  # +/- 6 % global atempo when pauses alone cannot hit the target
TARGET_TOLERANCE = 0.02  # within 2 % counts as "target met"

ProgressCallback = Callable[[ProgressEvent], None]
TimingMode = Literal["auto", "estimated", "aligned"]


@dataclass
class _Clip:
    unit: SynthUnit
    samples: np.ndarray  # 48 kHz, trimmed, engine-side tempo applied
    words: list[tuple[str, float, float]] | None  # relative to the raw (untrimmed) clip
    trim_offset: float
    tempo: float  # total tempo applied after trimming (raw seconds / final seconds)

    @property
    def seconds(self) -> float:
        return self.samples.size / SR


class _Reporter:
    def __init__(self, callback: ProgressCallback | None):
        self.callback = callback

    def __call__(self, stage: str, fraction: float, message: str = "", **detail) -> None:
        if self.callback:
            self.callback(
                ProgressEvent(stage=stage, fraction=min(1.0, max(0.0, fraction)),  # type: ignore[arg-type]
                              message=message, detail=detail)
            )


def _check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise JobCancelled("Cancelled.")


# ---------------------------------------------------------------- synthesis


def _synthesize_units(
    engine: TTSEngine,
    voice: VoiceSelection,
    units: list[SynthUnit],
    *,
    cache: SentenceCache | None,
    report: _Reporter,
    cancel: threading.Event | None,
    base_fraction: float,
    span_fraction: float,
) -> list[_Clip]:
    """Synthesize every unit (cache first), returning trimmed 48 kHz clips with engine tempo."""
    caps = engine.capabilities
    engine_speed = voice.speed if caps.native_speed else 1.0
    engine_tempo = 1.0 if caps.native_speed else voice.speed
    key_energy = voice.energy if caps.energy_control != "none" else None
    engine_voice = voice.model_copy(update={"speed": engine_speed})
    voice_key = engine.cache_voice_key(voice)
    done = 0
    lock = threading.Lock()

    def work(unit: SynthUnit) -> _Clip:
        nonlocal done
        _check(cancel)
        key = make_key(engine.id, engine.version, voice_key, engine_speed, key_energy, unit.text)
        cached = cache.get(key) if cache else None
        if cached is None:
            audio: SentenceAudio = engine.synthesize(unit.text, engine_voice)
            if audio.samples.size == 0:
                raise EngineError(f"The voice produced no audio for: {unit.text[:60]!r}")
            samples = ap.resample(audio.samples, audio.sample_rate, SR)
            samples, trim_offset = ap.trim_silence(samples, SR)
            cached = CachedClip(samples, audio.words, trim_offset)
            if cache:
                cache.put(key, cached)
        samples = ap.change_tempo(cached.samples, engine_tempo, SR) if engine_tempo != 1.0 else cached.samples
        with lock:
            done += 1
            report(
                "voice",
                base_fraction + span_fraction * done / len(units),
                f"Generating voice (sentence {done}/{len(units)})",
                done=done, total=len(units),
            )  # fmt: skip
        return _Clip(unit, samples, cached.words, cached.trim_offset, engine_tempo)

    clips: list[_Clip] = []
    workers = max(1, min(engine.max_parallel, len(units)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, u) for u in units]
        try:
            for f in futures:
                clips.append(f.result())
        except BaseException:
            for f in futures:
                f.cancel()
            raise
    return clips


# ------------------------------------------------------------------ timing


def _word_timings(
    analysis: ScriptAnalysis,
    clips: list[_Clip],
    spans: list[tuple[float, float]],
    mode: TimingMode,
    report: _Reporter,
    cancel: threading.Event | None,
) -> list[WordTiming]:
    use_align = mode == "aligned" or (mode == "auto" and aligner.is_available())
    words: list[WordTiming] = []
    for i, (clip, (start, end)) in enumerate(zip(clips, spans, strict=True)):
        _check(cancel)
        lo, hi = clip.unit.token_start, clip.unit.token_end
        tokens = analysis.tokens[lo:hi]
        if clip.words and mode != "estimated":
            part = native_timings(
                tokens, lo, clip.words, clip_start=start, trim_offset=clip.trim_offset,
                tempo=clip.tempo, clip_duration=end - start,
            )  # fmt: skip
        elif use_align and mode != "estimated":
            try:
                found = aligner.align_clip(clip.samples, SR, _align_language(analysis))
            except Exception as exc:  # alignment is an upgrade, never a reason to fail
                log.warning("Alignment failed (%s); using estimated timings", exc)
                found = []
            if found:
                part = native_timings(
                    tokens, lo, found, clip_start=start, trim_offset=0.0, tempo=1.0,
                    clip_duration=end - start, source="aligned",
                )  # fmt: skip
            else:
                part = estimate_timings(tokens, lo, (start, end))
        else:
            part = estimate_timings(tokens, lo, (start, end))
        words.extend(part)
        report("timing", 0.80 + 0.05 * (i + 1) / len(clips), "Timing words")
    return words


def _align_language(analysis: ScriptAnalysis) -> str | None:
    return {"en": "en", "hi": "hi"}.get(analysis.language_hint)


# -------------------------------------------------------------------- main


def speak(
    script: str | ScriptAnalysis,
    voice: VoiceSelection,
    workspace: Workspace | Path,
    *,
    target_seconds: float | None = None,
    pause_scale: float | None = None,
    plan_option: Literal["adjust_delivery", "speed_only"] = "adjust_delivery",
    timing: TimingMode = "auto",
    loudness: float | None = None,
    energy_processing: bool = True,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    engine: TTSEngine | None = None,
    cache: SentenceCache | None | Literal[False] = None,
) -> SynthesisResult:
    """Turn a script into narration. See module docstring and NarraReel WHAT_TO_DO.md 5.2.4 / 5.6."""
    ws = workspace if isinstance(workspace, Workspace) else Workspace(workspace)
    report = _Reporter(progress)
    report("prepare", 0.0, "Analysing script")

    analysis = script if isinstance(script, ScriptAnalysis) else analyze_script(script)
    if analysis.word_count == 0:
        raise ScriptError("Your script is empty. Paste or type some text first.")

    eng = engine or resolve_voice(voice)[0]
    status = eng.status()
    if not status.available:
        raise EngineUnavailable(status.reason or f"Voice engine '{eng.id}' is not available.")
    if status.needs_download:
        raise EngineUnavailable(
            f"{status.reason or 'A model download is needed.'} "
            f"Run `snipmint prepare {eng.id}` first (~{status.download_size_mb} MB)."
        )

    units = synthesis_units(analysis)
    cps, basis = dur.get_cps(voice.ref, analysis.language_hint)
    speed = voice.speed
    scale = pause_scale if pause_scale is not None else 1.0
    if target_seconds:
        plan = dur.plan(analysis, cps, target_seconds, basis)
        option = next(o for o in plan.options if o.kind == plan_option)
        speed, scale = option.speed or speed, option.pause_scale or scale
        log.info("Target %.1fs -> speed %.3f, pause scale %.2f", target_seconds, speed, scale)

    used_voice = voice
    cache_obj = None if cache is False else (cache or SentenceCache())

    with ws.logging():
        report("prepare", 0.02, "Preparing voice")
        lo_total = dur.layout_total(analysis, units, dur.SCALE_MIN)
        hi_total = dur.layout_total(analysis, units, dur.SCALE_MAX)

        def tempo_fit(speech: float) -> tuple[float, bool]:
            """Global tempo factor needed so pauses can absorb the rest, and whether it suffices."""
            budget = target_seconds - speech  # type: ignore[operator]
            if lo_total <= budget <= hi_total:
                return 1.0, True
            edge_total = hi_total if budget > hi_total else lo_total
            needed = target_seconds - edge_total  # type: ignore[operator]
            wanted = speech / needed if needed > 0 else 1 + 10 * MAX_TEMPO_CORRECTION
            factor = min(1 + MAX_TEMPO_CORRECTION, max(1 - MAX_TEMPO_CORRECTION, wanted))
            return factor, abs(wanted - factor) < 1e-9

        for attempt in range(2):
            used_voice = used_voice.model_copy(update={"speed": min(2.0, max(0.5, speed))})
            clips = _synthesize_units(
                eng, used_voice, units, cache=cache_obj, report=report, cancel=cancel,
                base_fraction=0.02, span_fraction=0.76,
            )  # fmt: skip
            speech_actual = sum(c.seconds for c in clips)
            if not target_seconds or attempt == 1:
                break
            planned = dur.speech_seconds(analysis, cps, speed)
            if abs(speech_actual - planned) <= 0.03 * planned and tempo_fit(speech_actual)[1]:
                break
            # The estimate was off (uncalibrated voice): re-plan with the measured speaking
            # rate and synthesize once more at the corrected speed.
            measured_cps = analysis.char_count / (speech_actual * used_voice.speed)
            replan = dur.plan(analysis, measured_cps, target_seconds, "calibrated")
            option = next(o for o in replan.options if o.kind == plan_option)
            new_speed = option.speed or speed
            log.info("Estimate missed; re-planning speed %.3f -> %.3f", speed, new_speed)
            if abs(new_speed - speed) < 0.02:
                break
            speed, scale = new_speed, option.pause_scale or scale

        # ---- closed loop: hit the target with pauses first, then a small global tempo change
        if target_seconds:
            factor, _ = tempo_fit(speech_actual)
            if abs(factor - 1.0) > 1e-6:
                log.info("Pauses cannot reach the target; applying tempo x%.3f", factor)
                for clip in clips:
                    clip.samples = ap.change_tempo(clip.samples, factor, SR)
                    clip.tempo *= factor
                speech_actual = sum(c.seconds for c in clips)
            scale = dur.solve_pause_scale(analysis, units, target_seconds - speech_actual)

        lead, pauses, tail = dur.pause_layout(analysis, units, scale)
        audio, spans = ap.assemble([c.samples for c in clips], pauses, lead, tail, SR)

        # ---- word timings (before post-processing; filters keep the length)
        words = _word_timings(analysis, clips, spans, timing, report, cancel)
        sentence_bounds: dict[int, tuple[float, float]] = {}
        for clip, (s, e) in zip(clips, spans, strict=True):
            lo_s, hi_s = sentence_bounds.get(clip.unit.sentence_idx, (s, e))
            sentence_bounds[clip.unit.sentence_idx] = (min(lo_s, s), max(hi_s, e))
        limits = [sentence_bounds[analysis.tokens[w.token_idx].sentence_idx] for w in words]
        words = enforce_quality(words, limits)

        # ---- post-processing
        report("voice", 0.86, "Polishing audio")
        _check(cancel)
        if energy_processing:
            audio = ap.apply_energy(audio, voice.energy, SR)
        lufs = config.load_settings().loudness_lufs if loudness is None else loudness
        audio = ap.loudnorm_two_pass(audio, SR, target=lufs)
        duration = audio.size / SR

        wav = ws.file("voice.wav")
        mp3 = ws.file("voice.mp3")
        ap.write_wav(wav, audio, SR)
        ap.encode_mp3(wav, mp3)
        ws.write_json("words.json", [w.model_dump() for w in words])
        ws.file("captions.srt").write_text(build_srt(analysis, words), encoding="utf-8")

        sentence_spans = [sentence_bounds[s.idx] for s in analysis.sentences]
        met, note = _target_report(analysis, duration, target_seconds)
        ws.write_project(
            {
                "script": analysis.text,
                "voice": used_voice.model_dump(),
                "target_seconds": target_seconds,
                "speed": speed,
                "pause_scale": scale,
                "duration": duration,
                "target_met": met,
            }
        )
        _update_calibration(voice, analysis, clips, speed)

    report("done", 1.0, "Done", duration=duration)
    return SynthesisResult(
        audio_wav=wav,
        audio_mp3=mp3,
        duration=duration,
        sample_rate=SR,
        words=words,
        sentence_spans=sentence_spans,
        target_seconds=target_seconds,
        target_met=met,
        note=note,
        speed=speed,
        pause_scale=scale,
    )


def _target_report(
    analysis: ScriptAnalysis, duration: float, target: float | None
) -> tuple[bool | None, str | None]:
    if not target:
        return None, None
    if abs(duration - target) <= TARGET_TOLERANCE * target:
        return True, None
    wps = analysis.word_count / duration if duration > 0 else 0
    words = round(abs(duration - target) * wps)
    verb = "Cut" if duration > target else "Add"
    return False, (
        f"Your audio is {dur.format_duration(duration)} (target {dur.format_duration(target)}). "
        f"{verb} about {words} words for an exact fit."
    )


def _update_calibration(
    voice: VoiceSelection, analysis: ScriptAnalysis, clips: list[_Clip], speed: float
) -> None:
    speech = sum(c.seconds for c in clips)
    if analysis.char_count < 100 or speech <= 0:
        return
    try:
        dur.update_calibration(voice.ref, analysis.char_count, speech, max(speed, 0.01))
    except Exception as exc:  # pragma: no cover - calibration is best effort
        log.debug("Calibration update skipped: %s", exc)


# ---------------------------------------------------------------- previews


def preview_audio(
    script: str | ScriptAnalysis,
    voice: VoiceSelection,
    *,
    max_sentences: int = 2,
    engine: TTSEngine | None = None,
    cache: SentenceCache | None | Literal[False] = None,
) -> np.ndarray:
    """The first sentence or two with the current voice settings (48 kHz float32)."""
    analysis = script if isinstance(script, ScriptAnalysis) else analyze_script(script)
    if analysis.word_count == 0:
        raise ScriptError("Your script is empty. Paste or type some text first.")
    eng = engine or resolve_voice(voice)[0]
    status = eng.status()
    if not status.available or status.needs_download:
        raise EngineUnavailable(status.reason or f"Voice engine '{eng.id}' is not ready.")
    units = [u for u in synthesis_units(analysis) if u.sentence_idx < max_sentences]
    cache_obj = None if cache is False else (cache or SentenceCache())
    clips = _synthesize_units(
        eng, voice, units, cache=cache_obj, report=_Reporter(None), cancel=None,
        base_fraction=0.0, span_fraction=1.0,
    )  # fmt: skip
    _, pauses, _ = dur.pause_layout(analysis, units, 1.0)
    audio, _ = ap.assemble([c.samples for c in clips], pauses, 0.05, 0.15, SR)
    return ap.apply_energy(audio, voice.energy, SR)
