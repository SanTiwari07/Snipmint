"""Run moment analysis for a source with the chosen provider, falling back to the free one."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from ..errors import AnalysisError
from ..ingest.source import Source
from ..transcribe.models import Transcript
from .providers import heuristic
from .schemas import AnalysisResult, AnalyzeOptions, Moment, Provider
from .signals import Signals

log = logging.getLogger("snipmint.analyze")

ProviderFn = Callable[..., list[Moment]]


def available_providers(gemini_key: str | None = None) -> dict[str, bool]:
    from . import providers as _p  # noqa: F401  (import side effects: none)

    return {
        "heuristic": True,
        "gemini": bool(gemini_key or os.environ.get("GEMINI_API_KEY")) and _gemini_installed(),
        "local_llm": bool((os.environ.get("LLM_BASE_URL") or "").strip()),
    }


def _gemini_installed() -> bool:
    try:
        import google.genai  # noqa: F401
    except Exception:
        return False
    return True


def resolve_provider(choice: str, gemini_key: str | None = None) -> Provider:
    avail = available_providers(gemini_key)
    if choice == "auto":
        if avail["gemini"]:
            return "gemini"
        if avail["local_llm"]:
            return "local_llm"
        return "heuristic"
    return choice  # type: ignore[return-value]


def _provider_fn(name: Provider) -> ProviderFn:
    if name == "heuristic":
        return heuristic.pick_moments
    if name == "gemini":
        from .providers import gemini

        return gemini.pick_moments
    if name == "local_llm":
        from .providers import local_llm

        return local_llm.pick_moments
    raise AnalysisError(f"Unknown analysis provider: {name}")


def load_inputs(source: Source) -> tuple[Transcript, Signals]:
    if not source.file("transcript.json").is_file():
        raise AnalysisError("This video has not been transcribed yet.")
    transcript = Transcript.model_validate(source.read_json("transcript.json"))
    signals = (
        Signals.model_validate(source.read_json("signals.json"))
        if source.file("signals.json").is_file()
        else Signals()
    )
    return transcript, signals


def analyze(
    source: Source,
    opts: AnalyzeOptions,
    *,
    gemini_key: str | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> AnalysisResult:
    meta = source.meta
    if meta is None:
        raise AnalysisError("This video has no metadata yet.")
    transcript, signals = load_inputs(source)
    provider = resolve_provider(opts.provider, gemini_key)
    reason: str | None = None
    moments: list[Moment]
    cost: float | None = None

    if provider != "heuristic":
        try:
            fn = _provider_fn(provider)
            if progress:
                progress(0.1, "Asking Gemini to pick the best moments" if provider == "gemini"
                         else "Asking the local AI to pick the best moments")  # fmt: skip
            out = fn(transcript, signals, meta, opts, api_key=gemini_key) if provider == "gemini" \
                else fn(transcript, signals, meta, opts)  # fmt: skip
            if isinstance(out, tuple):
                moments, cost = out
            else:
                moments = out
            if not moments:
                raise AnalysisError("the model returned no clips")
        except Exception as exc:  # any failure: fall back to the free analysis
            label = "Gemini" if provider == "gemini" else "Local AI"
            reason = f"{label} failed: {_short(exc)}"
            log.warning("%s; falling back to the free analysis", reason)
            provider = "heuristic"

    if provider == "heuristic":
        if progress:
            progress(0.5, "Scoring moments")
        moments = heuristic.pick_moments(transcript, signals, meta, opts)
        if reason:
            for m in moments:
                m.fallback = True

    for m in moments:
        if not m.text:
            m.text = " ".join(w.text.strip() for w in transcript.words_between(m.start, m.end))
    result = AnalysisResult(provider=provider, requested=opts.provider, fallback_reason=reason,
                            options=opts, moments=moments, cost_usd=cost)  # fmt: skip
    source.write_json(source.moments_file(provider).name, result.model_dump())
    source.write_json("moments.latest.json", result.model_dump())
    return result


def _short(exc: Exception) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    for marker in ("AIza",):  # never echo a key back
        if marker in text:
            text = "invalid or rejected API key"
    return text[:200]


def latest(source: Source) -> AnalysisResult | None:
    f = source.file("moments.latest.json")
    if not f.is_file():
        return None
    try:
        return AnalysisResult.model_validate(source.read_json(f.name))
    except ValueError:
        return None
