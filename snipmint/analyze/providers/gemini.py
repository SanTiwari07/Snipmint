"""Advanced mode: Gemini picks the moments and writes titles, hooks and descriptions.

Needs ``pip install "snipmint[gemini]"`` and an API key (from the UI or GEMINI_API_KEY).
Only transcript text and a few numbers per window are sent; never the video.
The call/retry/block handling follows openshorts/main.py ``_run_gemini_stage`` and
openshorts/gemini_worker.py ``raise_if_blocked`` (MIT, (c) 2024 OpenShorts).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ...audio import ffmpeg as ff
from ...errors import AnalysisError, DependencyMissing
from ...ingest.source import SourceMeta
from ...transcribe.models import Transcript
from .. import llm_pass, prompts
from ..schemas import AnalyzeOptions, Moment
from ..signals import Signals
from ..windows import auto_clip_count
from .heuristic import speech_is_sparse

log = logging.getLogger("snipmint.analyze.gemini")

DEFAULT_MODEL = "gemini-3.1-flash-lite"
INSTALL_HINT = 'Install with: pip install "snipmint[gemini]"'
_BLOCKED = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}
_TRANSIENT = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500", "INTERNAL", "overloaded",
              "Deadline", "empty response", "JSON")  # fmt: skip
_KEY_PATTERN = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")


def model_name() -> str:
    return (os.environ.get("GEMINI_MODEL") or "").strip() or DEFAULT_MODEL


def redact(text: str) -> str:
    """Never let an API key reach a log line or an error message."""
    return _KEY_PATTERN.sub("AIza…(hidden)", text)


def make_client(api_key: str):
    try:
        from google import genai
    except ImportError as exc:
        raise DependencyMissing("google-genai", INSTALL_HINT) from exc
    return genai.Client(api_key=api_key)


def _parse_text(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("Gemini returned an empty response body.")
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Gemini response did not contain a JSON object.")
    return json.loads(text[start : end + 1])


def _raise_if_blocked(response: Any) -> None:
    pf = getattr(response, "prompt_feedback", None)
    reason = getattr(pf, "block_reason", None)
    if reason:
        raise llm_pass.BlockedError(f"Gemini blocked this content ({getattr(reason, 'name', reason)}).")
    for c in getattr(response, "candidates", None) or []:
        fr = getattr(c, "finish_reason", None)
        name = (getattr(fr, "name", None) or str(fr or "")).upper()
        if name in _BLOCKED:
            raise llm_pass.BlockedError(f"Gemini blocked its answer ({name}).")


def make_call(client: Any, model: str, sleep=time.sleep, contents_prefix: list | None = None):
    """A ``call(prompt, schema)`` for llm_pass with structured output and transient retries."""

    def call(prompt: str, schema: type[BaseModel]) -> dict[str, Any]:
        try:
            from google.genai import types as gtypes

            config: Any = gtypes.GenerateContentConfig(response_mime_type="application/json",
                                                       response_schema=schema)  # fmt: skip
        except ImportError:  # tests with a fake client
            config = {"response_mime_type": "application/json", "response_schema": schema}
        for attempt in range(1, 4):
            try:
                contents = [*contents_prefix, prompt] if contents_prefix else prompt
                response = client.models.generate_content(model=model, contents=contents, config=config)
                _raise_if_blocked(response)
                parsed = getattr(response, "parsed", None)
                if parsed is not None:
                    data = parsed.model_dump() if hasattr(parsed, "model_dump") else parsed
                else:
                    data = _parse_text(getattr(response, "text", "") or "")
                return schema.model_validate(data).model_dump()
            except llm_pass.BlockedError:
                raise
            except Exception as exc:
                msg = redact(str(exc))
                if attempt == 3 or not any(t in msg for t in _TRANSIENT):
                    raise AnalysisError(f"Gemini error: {msg[:300]}") from None
                wait = 3 * (2 ** (attempt - 1))
                log.warning("Gemini transient error (attempt %d/3), retrying in %ds: %s", attempt, wait, msg[:150])
                sleep(wait)
        raise AnalysisError("Gemini did not answer.")

    return call


def pick_moments(
    transcript: Transcript, signals: Signals, meta: SourceMeta, opts: AnalyzeOptions,
    *, api_key: str | None = None, client: Any = None, model: str | None = None, video: Path | None = None,
) -> list[Moment]:  # fmt: skip
    key = (api_key or os.environ.get("GEMINI_API_KEY") or "").strip()
    if client is None:
        if not key:
            raise AnalysisError("no Gemini key on the server; add GEMINI_API_KEY to .env and restart Snipmint")
        client = make_client(key)
    model = (model or "").strip() or model_name()
    if speech_is_sparse(transcript, meta.duration):
        if video is None:
            raise AnalysisError("This video has almost no speech and no video file to watch.")
        log.info("Little speech: Gemini (%s) watches the video instead", model)
        return visual_moments(client, model, video, meta, opts, language=transcript.language)
    log.info("Analysing with Gemini (%s)", model)
    batch = int(os.environ.get("LLM_SCORE_BATCH") or 8)
    return llm_pass.pick_moments(make_call(client, model), "gemini", transcript, signals, meta, opts,
                                 batch_size=batch)  # fmt: skip


# ---------------------------------------------------------------------- silent videos


def max_video_minutes() -> float:
    """Gemini bills video at ~300 tokens/s; past ~45 min a 1M context runs out (OpenShorts had no
    guard here). Longer silent videos use the free analysis."""
    try:
        return float(os.environ.get("GEMINI_VIDEO_MAX_MINUTES") or 45)
    except ValueError:
        return 45.0


def _small_copy(video: Path) -> Path:
    """A 360p, low-frame-rate, silent copy: Gemini samples about one frame a second anyway."""
    out = video.with_name("gemini_upload.mp4")
    if not out.is_file():
        tmp = out.with_name("gemini_upload.tmp.mp4")
        ff.run_ffmpeg(["-v", "error", "-i", str(video), "-vf", "scale=-2:360,fps=2", "-an",
                       "-c:v", "libx264", "-preset", "veryfast", "-crf", "32", str(tmp)])  # fmt: skip
        tmp.replace(out)
    return out


def visual_moments(client: Any, model: str, video: Path, meta: SourceMeta, opts: AnalyzeOptions,
                   *, language: str = "en", sleep=time.sleep) -> list[Moment]:  # fmt: skip
    """Silent footage (gaming, music, b-roll): Gemini watches a small copy and picks moments."""
    if meta.duration > max_video_minutes() * 60:
        raise AnalysisError(
            f"Gemini can watch silent videos up to {max_video_minutes():.0f} minutes "
            "(GEMINI_VIDEO_MAX_MINUTES); this one is longer."
        )
    count = opts.count or auto_clip_count(meta.duration)
    upload = client.files.upload(file=str(_small_copy(video)))
    try:
        deadline = time.time() + 300
        while True:
            info = client.files.get(name=upload.name)
            raw_state = getattr(info, "state", "")
            state = str(getattr(raw_state, "name", raw_state)).upper()
            if state.endswith("ACTIVE"):
                break
            if state.endswith("FAILED") or time.time() > deadline:
                raise AnalysisError("Gemini could not process the video.")
            sleep(2)
        prompt = prompts.VISUAL_PROMPT_TEMPLATE.format(
            min_clips=max(1, count - 1), max_clips=count, video_duration=round(meta.duration, 3),
            min_secs=opts.min_seconds, max_secs=opts.max_seconds, language=language or "en", title=meta.title,
        )  # fmt: skip
        data = make_call(client, model, sleep=sleep, contents_prefix=[upload])(prompt, prompts.VisualResponse)
    finally:
        with contextlib.suppress(Exception):  # the upload expires on its own anyway
            client.files.delete(name=upload.name)
    out: list[Moment] = []
    for c in sorted(data.get("shorts") or [], key=lambda x: -int(x.get("predicted_score") or 0)):
        start, end = max(0.0, float(c["start"])), min(meta.duration, float(c["end"]))
        if end - start < 1 or any(min(end, m.end) - max(start, m.start) > 0.25 * (end - start) for m in out):
            continue
        out.append(Moment(
            id=f"m{len(out) + 1:02d}", start=round(start, 3), end=round(end, 3),
            score=max(0, min(100, int(c.get("predicted_score") or 50))), why=str(c.get("why") or ""),
            title=str(c.get("video_title_for_youtube_short") or "")[:100],
            hook_text=str(c.get("viral_hook_text") or ""),
            descriptions={"tiktok": str(c.get("video_description_for_tiktok") or ""),
                          "instagram": str(c.get("video_description_for_instagram") or "")},
            provider="gemini",
        ))  # fmt: skip
    return out


# ---------------------------------------------------------------------- caption proofreading


def proofread(client: Any, model: str, transcript: Transcript, moments: list[Moment], title: str,
              *, max_words: int = 4000) -> dict[int, str]:  # fmt: skip
    """Ask Gemini which words of the picked clips Whisper misheard (names, brands, terms).

    Returns {global word index: corrected word}. Only single-word swaps are accepted, so the word
    timings, and the captions built on them, stay exactly as they were."""
    words = transcript.words
    clips, total = [], 0
    for m in moments:
        idx = [i for i, w in enumerate(words) if m.start <= (w.start + w.end) / 2 < m.end]
        if total + len(idx) > max_words:
            break
        total += len(idx)
        clips.append([[i, words[i].text.strip()] for i in idx])
    if not any(clips):
        return {}
    prompt = prompts.FIX_PROMPT_TEMPLATE.format(title=title, language=transcript.language,
                                                clips_json=json.dumps(clips, ensure_ascii=False))  # fmt: skip
    data = make_call(client, model)(prompt, prompts.FixResponse)
    allowed = {i for clip in clips for i, _ in clip}
    fixes: dict[int, str] = {}
    for f in data.get("fixes") or []:
        i, text = int(f["i"]), str(f["text"]).strip()
        old = words[i].text.strip() if 0 <= i < len(words) else ""
        if i in allowed and text and " " not in text and text != old and len(text) <= 3 * len(old) + 12:
            fixes[i] = text
    return fixes


def check_key(api_key: str) -> tuple[bool, str | None]:
    """Cheap validity check: list models with this key."""
    try:
        client = make_client(api_key)
        models = client.models.list(config={"page_size": 5})
        next(iter(models), None)
        return True, None
    except DependencyMissing as exc:
        return False, str(exc)
    except Exception as exc:
        msg = redact(str(exc))
        if "API_KEY_INVALID" in msg or "API key not valid" in msg or "PERMISSION_DENIED" in msg:
            return False, "Google did not accept this key."
        return False, f"Could not check the key: {msg[:200]}"
