"""Sentence-anchored word timing estimation, no extra dependencies (section 5.7.2).

Because every sentence is synthesized separately and trimmed, its exact start and end are
known; only the split *inside* a sentence is estimated, so errors cannot accumulate.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from ..models import Token, WordTiming

_VOWEL_GROUPS = re.compile(r"[aeiouy]+")
_SOFT_PUNCT = (",", ";", ":")
_HARD_PUNCT = (".", "!", "?", "…", "।", "॥")
_CLOSERS = "\"'”’)]"


def syllable_estimate(text: str) -> float:
    """Rough spoken length of a token in 'syllables' (min 1)."""
    letters = [ch for ch in text if ch.isalpha()]
    digits = sum(ch.isdigit() for ch in text)
    devanagari = [ch for ch in letters if "ऀ" <= ch <= "ॿ"]
    if devanagari:
        # one base letter per consonant+vowel-sign cluster; a virama joins two letters
        joined = text.count("्")
        return max(1.0, len(devanagari) - joined) + digits * 1.5
    latin = "".join(ch for ch in unicodedata.normalize("NFKD", "".join(letters)) if ch.isascii())
    latin = latin.lower()
    count = len(_VOWEL_GROUPS.findall(latin))
    if count > 1 and latin.endswith("e") and not latin.endswith(("le", "ee", "ie")):
        count -= 1  # silent final e
    base = max(1.0, float(count)) if latin else 0.0
    return max(1.0, base + digits * 1.5)


def _punct_weight(text: str) -> float:
    stripped = text.rstrip(_CLOSERS)
    if stripped.endswith(_HARD_PUNCT):
        return 1.0
    if stripped.endswith(_SOFT_PUNCT):
        return 0.5
    return 0.0


def estimate_timings(
    tokens: Sequence[Token],
    first_token_idx: int,
    span: tuple[float, float],
) -> list[WordTiming]:
    """Spread the words of one unit across ``span`` (start, end of the spoken audio)."""
    if not tokens:
        return []
    syl = [syllable_estimate(t.text) for t in tokens]
    gap = [_punct_weight(t.text) for t in tokens]
    gap[-1] = 0.0  # speech ends with the last word; trailing silence was trimmed
    total = sum(syl) + sum(gap)
    start, end = span
    unit = (end - start) / total if total > 0 else 0.0
    out: list[WordTiming] = []
    cursor = start
    for k, tok in enumerate(tokens):
        w_start = cursor
        w_end = w_start + syl[k] * unit
        cursor = w_end + gap[k] * unit
        out.append(
            WordTiming(
                token_idx=first_token_idx + k,
                text=tok.text,
                start=w_start,
                end=min(w_end, end),
                source="estimated",
            )
        )
    return out


def estimate_script_timings(
    analysis,
    cps: float | None = None,
    speed: float = 1.0,
    pause_scale: float = 1.0,
) -> tuple[list[WordTiming], float]:
    """Timings for a whole script without any audio (used for caption previews).

    Returns the words and the estimated total length in seconds.
    """
    from ..script import duration as dur
    from ..script.analyze import synthesis_units

    cps = cps or dur.default_cps(analysis.language_hint)
    units = synthesis_units(analysis)
    lead, pauses, tail = dur.pause_layout(analysis, units, pause_scale)
    cursor = lead
    words: list[WordTiming] = []
    for i, unit in enumerate(units):
        tokens = analysis.tokens[unit.token_start : unit.token_end]
        chars = sum(1 for t in tokens for ch in t.text if ch.isalnum())
        seconds = max(0.15, chars / (cps * speed))
        words.extend(estimate_timings(tokens, unit.token_start, (cursor, cursor + seconds)))
        cursor += seconds + (pauses[i] if i < len(pauses) else 0.0)
    return words, cursor + tail
