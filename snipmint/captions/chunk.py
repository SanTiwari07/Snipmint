"""Group timed words into on-screen caption chunks (NarraReel NarraReel WHAT_TO_DO.md section 5.9.1)."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from ..models import ScriptAnalysis, WordTiming
from ..script.analyze import _is_emoji
from .styles import CaptionStyle

BREAK_GAP = 0.35  # a pause longer than this always starts a new chunk
HOLD = 0.15  # how long the last word stays on screen after it is spoken
MIN_ON_SCREEN = 0.4
SOFT_BREAKS = (",", ";", ":", "—", "–")


@dataclass
class CapWord:
    text: str  # as displayed (emoji stripped, uppercase applied)
    start: float
    end: float
    sentence_idx: int
    emphasis: bool = False


@dataclass
class CaptionChunk:
    words: list[CapWord]
    lines: list[list[int]]  # indices into ``words``, one list per rendered line
    start: float
    end: float
    sentence_idx: int = 0
    _meta: dict = field(default_factory=dict, repr=False)


def display_text(raw: str, uppercase: bool, keep_emoji: bool = False) -> str:
    text = raw if keep_emoji else "".join(ch for ch in raw if not _is_emoji(ch) and ch != "️")
    text = re.sub(r"\s+", " ", text).strip()
    return text.upper() if uppercase else text


def auto_emphasis(text: str) -> bool:
    """Numbers, long words and ALL-CAPS words stand out without any markup."""
    letters = [c for c in text if c.isalpha()]
    return (
        any(c.isdigit() for c in text)
        or len(letters) >= 8
        or (len(letters) >= 2 and all(c.isupper() for c in letters))
    )


def build_cap_words(
    words: Sequence[WordTiming], analysis: ScriptAnalysis, style: CaptionStyle, keep_emoji: bool = False
) -> list[CapWord]:
    out: list[CapWord] = []
    for w in words:
        text = display_text(w.text, style.font.uppercase, keep_emoji)
        if not text or not any(ch.isalnum() for ch in text):
            # nothing but punctuation/emoji left: glue onto the previous word's timing
            if out and text:
                out[-1].text += " " + text
                out[-1].end = max(out[-1].end, w.end)
            continue
        token = analysis.tokens[w.token_idx]
        emph = token.emphasis
        if style.emphasis.enabled and style.emphasis.auto and not emph:
            emph = auto_emphasis(re.sub(r"[^\w]", "", text))
        if not style.emphasis.enabled:
            emph = False
        out.append(CapWord(text, w.start, w.end, token.sentence_idx, emph))
    return out


# ---------------------------------------------------------------- wrapping

MeasureFn = Callable[[CapWord], float]


def fit_lines(
    widths: Sequence[float], space: float, max_width: float, max_lines: int
) -> list[list[int]] | None:
    """Break word indices into at most ``max_lines`` balanced lines; None if they don't fit."""
    n = len(widths)
    if n == 0:
        return []

    def line_w(i: int, j: int) -> float:
        return sum(widths[i:j]) + space * (j - i - 1)

    if line_w(0, n) <= max_width:
        return [list(range(n))]
    if max_lines < 2 or n < 2:
        return None
    best, best_w = None, None
    for cut in range(1, n):
        a, b = line_w(0, cut), line_w(cut, n)
        if max(a, b) <= max_width and (best_w is None or max(a, b) < best_w):
            best, best_w = cut, max(a, b)
    if best is None:
        return None
    return [list(range(best)), list(range(best, n))]


def chunk_words(
    words: Sequence[CapWord],
    style: CaptionStyle,
    word_width: MeasureFn,
    space_width: float,
    max_width: float,
) -> list[CaptionChunk]:
    wpc = style.layout.words_per_chunk
    max_lines = style.layout.max_lines

    def widths_of(group: Sequence[CapWord]) -> list[float]:
        return [word_width(w) for w in group]

    def fits(group: Sequence[CapWord]) -> bool:
        return fit_lines(widths_of(group), space_width, max_width, max_lines) is not None

    groups: list[list[CapWord]] = []
    cur: list[CapWord] = []
    for w in words:
        if cur and (
            w.sentence_idx != cur[-1].sentence_idx
            or w.start - cur[-1].end > BREAK_GAP
            or len(cur) >= wpc
            or not fits([*cur, w])
        ):
            groups.append(cur)
            cur = []
        cur.append(w)
        if wpc > 1 and len(cur) >= 2 and cur[-1].text.rstrip("\"'”’)]").endswith(SOFT_BREAKS):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    # Orphans: a lone short word is glued to a neighbour in the same sentence if it still fits.
    if wpc > 1:
        i = 0
        while i < len(groups):
            g = groups[i]
            if len(g) == 1 and len(re.sub(r"\W", "", g[0].text)) <= 3:
                prev_ok = (
                    i > 0
                    and groups[i - 1][-1].sentence_idx == g[0].sentence_idx
                    and g[0].start - groups[i - 1][-1].end <= BREAK_GAP
                    and fits([*groups[i - 1], g[0]])
                )
                next_ok = (
                    i + 1 < len(groups)
                    and groups[i + 1][0].sentence_idx == g[0].sentence_idx
                    and groups[i + 1][0].start - g[0].end <= BREAK_GAP
                    and fits([g[0], *groups[i + 1]])
                )
                if prev_ok:
                    groups[i - 1].append(g[0])
                    del groups[i]
                    continue
                if next_ok:
                    groups[i + 1].insert(0, g[0])
                    del groups[i]
                    continue
            i += 1

    chunks: list[CaptionChunk] = []
    for g in groups:
        lines = fit_lines(widths_of(g), space_width, max_width, max_lines)
        if lines is None:  # a single over-wide word: let it overflow on its own line
            lines = [list(range(len(g)))]
        chunks.append(CaptionChunk(g, lines, g[0].start, g[-1].end, g[0].sentence_idx))

    for i, c in enumerate(chunks):
        nxt = chunks[i + 1].start if i + 1 < len(chunks) else None
        end = c.words[-1].end + HOLD
        if end - c.start < MIN_ON_SCREEN:
            end = c.start + MIN_ON_SCREEN
        if nxt is not None:
            end = min(end, nxt)
        c.end = max(end, c.start + 0.05)
    return chunks
