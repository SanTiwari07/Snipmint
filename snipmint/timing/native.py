"""Map words reported by an engine (or an aligner) onto the user's script tokens.

Captions always show the script's own text and punctuation, so every :class:`WordTiming`
points at a script token (NarraReel NarraReel WHAT_TO_DO.md section 5.7.1).
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence

from ..models import Token, WordTiming
from ..script.analyze import fold

EngineWord = tuple[str, float, float]  # (text, start, end) in seconds


def _weights(tokens: Sequence[Token]) -> list[float]:
    return [max(1, len(t.norm)) for t in tokens]


def _fill_gaps(
    times: list[tuple[float, float] | None],
    tokens: Sequence[Token],
    span: tuple[float, float],
) -> list[tuple[float, float]]:
    """Interpolate tokens that no engine word matched."""
    weights = _weights(tokens)
    out: list[tuple[float, float]] = [(0.0, 0.0)] * len(times)
    i = 0
    prev_end = span[0]
    while i < len(times):
        if times[i] is not None:
            out[i] = times[i]  # type: ignore[assignment]
            prev_end = out[i][1]
            i += 1
            continue
        j = i
        while j < len(times) and times[j] is None:
            j += 1
        next_start = times[j][0] if j < len(times) else span[1]  # type: ignore[index]
        next_start = max(next_start, prev_end)
        total = sum(weights[i:j])
        cursor = prev_end
        for k in range(i, j):
            share = (next_start - prev_end) * weights[k] / total
            out[k] = (cursor, cursor + share)
            cursor += share
        prev_end = next_start
        i = j
    return out


def map_words_to_tokens(
    tokens: Sequence[Token],
    engine_words: Sequence[EngineWord],
    span: tuple[float, float],
) -> list[tuple[float, float]]:
    """Return one (start, end) per token, in the same clock as ``engine_words``.

    ``span`` bounds the unit (used when nothing matches or to place unmatched words).
    """
    if not tokens:
        return []
    ew = [w for w in engine_words if fold(w[0])]
    if not ew:
        return _fill_gaps([None] * len(tokens), tokens, span)

    a = [t.norm for t in tokens]
    b = [fold(w[0]) for w in ew]
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    times: list[tuple[float, float] | None] = [None] * len(tokens)
    extra_after: dict[int, float] = {}  # token index -> end time of unmatched engine words

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                times[i1 + k] = (ew[j1 + k][1], ew[j1 + k][2])
        elif tag == "replace":
            if i2 - i1 == j2 - j1:
                for k in range(i2 - i1):
                    times[i1 + k] = (ew[j1 + k][1], ew[j1 + k][2])
            else:
                # e.g. "2026" spoken as several words, or several tokens read as one:
                # share the block's span between the tokens by length.
                start, end = ew[j1][1], ew[j2 - 1][2]
                w = _weights(tokens[i1:i2])
                total = sum(w)
                cursor = start
                for k in range(i2 - i1):
                    share = (end - start) * w[k] / total
                    times[i1 + k] = (cursor, cursor + share)
                    cursor += share
        elif tag == "insert" and i1 > 0:
            # extra spoken words with no script token: let the previous token absorb them
            extra_after[i1 - 1] = max(extra_after.get(i1 - 1, 0.0), ew[j2 - 1][2])
        # "delete": tokens nobody spoke - interpolated below

    for idx, end in extra_after.items():
        t = times[idx]
        if t is not None:
            times[idx] = (t[0], max(t[1], end))

    return _fill_gaps(times, tokens, span)


def native_timings(
    tokens: Sequence[Token],
    first_token_idx: int,
    engine_words: Sequence[EngineWord] | None,
    *,
    clip_start: float,
    trim_offset: float,
    tempo: float,
    clip_duration: float,
    source: str = "native",
) -> list[WordTiming]:
    """Engine word times (relative to the raw clip) -> WordTimings on the final timeline.

    ``trim_offset`` is the leading silence removed from the clip, ``tempo`` the speed-up
    applied afterwards (1.0 = none), ``clip_start`` where the clip begins in the narration.
    """
    clip_end = clip_start + clip_duration

    def to_final(t: float) -> float:
        return min(clip_end, max(clip_start, clip_start + (t - trim_offset) / tempo))

    moved = [(w, to_final(s), to_final(e)) for w, s, e in (engine_words or [])]
    spans = map_words_to_tokens(tokens, moved, (clip_start, clip_end))
    return [
        WordTiming(
            token_idx=first_token_idx + k,
            text=tok.text,
            start=s,
            end=e,
            source=source,  # type: ignore[arg-type]
        )
        for k, (tok, (s, e)) in enumerate(zip(tokens, spans, strict=True))
    ]
