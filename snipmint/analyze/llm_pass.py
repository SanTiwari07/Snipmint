"""Two-pass LLM moment picking, shared by the Gemini and local-LLM providers.

Adapted from openshorts/main.py ``get_viral_clips`` and openshorts/clip_selection.py
(``build_transcript_windows``, ``trim_to_best``, ``dedupe_overlapping``) (MIT, (c) 2024 OpenShorts).

Pass 1 scores every transcript window (the shortlist is the global top N, never "up to K per
batch"); pass 2 asks for exact clips with copy on the shortlist. A clip floor is enforced in
code: windows that produced nothing get one more call.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from ..ingest.source import SourceMeta
from ..transcribe.models import Transcript
from . import prompts
from .schemas import AnalyzeOptions, Moment, Provider
from .signals import Signals
from .windows import (
    auto_clip_count,
    clip_count_targets,
    overlap_ratio,
    shortlist_target,
    snap_to_words,
)

log = logging.getLogger("snipmint.analyze.llm")

# call(prompt, schema) -> parsed dict matching the schema
CallFn = Callable[[str, type[BaseModel]], dict[str, Any]]


class BlockedError(RuntimeError):
    """The model refused this content for policy reasons (deterministic: do not retry)."""


def build_windows(transcript: Transcript, window_seconds: float, overlap: float = 30.0) -> list[dict[str, Any]]:
    """Windows of whole segments, about ``window_seconds`` long, overlapping by ``overlap``."""
    segs = [(s.start, s.end, s.text.strip()) for s in transcript.segments if s.text.strip()]
    out: list[dict[str, Any]] = []
    i, n = 0, len(segs)
    while i < n:
        w_start = segs[i][0]
        j = i
        while j + 1 < n and segs[j + 1][1] - w_start <= window_seconds * 1.25:
            j += 1
            if segs[j][1] - w_start >= window_seconds:
                break
        out.append({"id": f"w{len(out) + 1:03d}", "start": round(w_start, 3), "end": round(segs[j][1], 3),
                    "text": " ".join(s[2] for s in segs[i : j + 1])})  # fmt: skip
        if j >= n - 1:
            break
        target = segs[j][1] - overlap
        k = i + 1
        while k <= j and segs[k][0] < target:
            k += 1
        i = max(k, i + 1)
    return out


def _hints(w: dict[str, Any], signals: Signals, meta: SourceMeta) -> dict[str, Any]:
    heat = signals.mean_heat(w["start"], w["end"])
    z = signals.loudness_z()
    energy = None
    if z.size:
        a, b = int(w["start"] / signals.hop), max(int(w["start"] / signals.hop) + 1, int(w["end"] / signals.hop))
        seg = z[a:b]
        energy = round(float(seg.mean()), 2) if seg.size else None
    mid = (w["start"] + w["end"]) / 2
    chapter = next((c.title for c in meta.chapters if c.start <= mid < c.end), None)
    return {"replay": None if heat is None else round(heat, 2), "energy": energy, "chapter": chapter}


def score_batches(items: list[Any], size: int) -> list[list[Any]]:
    """Near-equal batches (a trailing batch of one would get that window whatever its score)."""
    if not items:
        return []
    n_batches = max(1, -(-len(items) // max(1, size)))
    base, extra = divmod(len(items), n_batches)
    out, pos = [], 0
    for b in range(n_batches):
        k = base + (1 if b < extra else 0)
        out.append(items[pos : pos + k])
        pos += k
    return out


def _split_call(call: CallFn, items: list[dict], build: Callable[[list[dict]], str],
                schema: type[BaseModel], key: str) -> list[dict]:  # fmt: skip
    """Call the model; on a policy block, bisect so one bad window doesn't sink the batch."""
    if not items:
        return []
    try:
        return list(call(build(items), schema).get(key) or [])
    except BlockedError:
        if len(items) == 1:
            log.warning("Model blocked window %s on its own; skipping it", items[0].get("id"))
            return []
        mid = len(items) // 2
        return _split_call(call, items[:mid], build, schema, key) + _split_call(call, items[mid:], build, schema, key)


def pick_moments(
    call: CallFn,
    provider: Provider,
    transcript: Transcript,
    signals: Signals,
    meta: SourceMeta,
    opts: AnalyzeOptions,
    *,
    batch_size: int = 8,
) -> list[Moment]:
    duration = meta.duration or transcript.duration
    language = transcript.language or "unknown"
    min_s, max_s = opts.min_seconds, opts.max_seconds
    windows = build_windows(transcript, window_seconds=max(90.0, max_s * 1.5))
    if not windows:
        return []
    for w in windows:
        w["hints"] = _hints(w, signals, meta)

    def payload(ws: list[dict]) -> str:
        return json.dumps([{k: w[k] for k in ("id", "start", "end", "text", "hints")} for w in ws], ensure_ascii=False)

    def score_prompt(ws: list[dict]) -> str:
        return prompts.SCORE_PROMPT_TEMPLATE.format(
            language=language, title=meta.title, video_duration=round(duration), windows_json=payload(ws))

    scored: list[dict] = []
    for batch in score_batches(windows, batch_size):
        scored.extend(_split_call(call, batch, score_prompt, prompts.ScoreResponse, "windows"))
    scored.sort(key=lambda s: int(s.get("score") or 0), reverse=True)
    by_id = {w["id"]: w for w in windows}
    target = max(shortlist_target(duration), opts.count or 0)
    shortlist = [by_id[s["id"]] for s in scored[:target] if s.get("id") in by_id] or windows[:target]

    low, high = clip_count_targets(len(shortlist))
    if opts.count:
        low = high = opts.count
    else:
        low = min(max(low, auto_clip_count(duration) // 2), high)

    def detail_prompt(lo: int, hi: int) -> Callable[[list[dict]], str]:
        def build(ws: list[dict]) -> str:
            return prompts.DETAIL_PROMPT_TEMPLATE.format(
                language=language, title=meta.title, video_duration=round(duration),
                min_clips=lo, max_clips=hi, min_secs=min_s, max_secs=max_s, windows_json=payload(ws),
            )  # fmt: skip
        return build

    shorts = _split_call(call, shortlist, detail_prompt(low, high), prompts.DetailResponse, "shorts")
    if len(shorts) < low:  # the floor is enforced in code, not only in the prompt
        used = {str(s.get("source_window_id") or "") for s in shorts}
        spare = [w for w in shortlist if w["id"] not in used]
        if spare:
            missing = low - len(shorts)
            shorts += _split_call(call, spare, detail_prompt(missing, max(missing, len(spare))),
                                  prompts.DetailResponse, "shorts")  # fmt: skip

    words = transcript.words
    clips: list[Moment] = []
    for s in sorted(shorts, key=lambda x: -int(x.get("predicted_score") or 0)):
        try:
            d = prompts.DetailClip.model_validate(s)
        except ValueError:
            continue
        start, end = max(0.0, min(d.start, d.end)), min(duration or d.end, max(d.start, d.end))
        if end - start < 3:
            continue
        start, end = snap_to_words(start, end, words, duration)
        if any(overlap_ratio((start, end), (m.start, m.end)) > 0.25 for m in clips):
            continue  # keep the better-scored of two overlapping clips
        clips.append(Moment(
            id="", start=start, end=end, score=max(0, min(100, d.predicted_score)),
            why=d.why.strip(), title=d.video_title_for_youtube_short.strip()[:100],
            hook_text=d.viral_hook_text.strip(),
            descriptions={"tiktok": d.video_description_for_tiktok.strip(),
                          "instagram": d.video_description_for_instagram.strip()},
            keywords=[k.strip() for k in d.keywords if k.strip()][:6], provider=provider,
        ))  # fmt: skip
        if len(clips) >= high:
            break
    for i, m in enumerate(clips, 1):
        m.id = f"m{i:02d}"
    return clips
