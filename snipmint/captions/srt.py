"""SRT subtitles (basic chunker; Phase 2 reuses ``chunk.py`` for styled captions)."""

from __future__ import annotations

from collections.abc import Sequence

from ..models import ScriptAnalysis, WordTiming

MAX_LINE_CHARS = 42
MAX_CUE_CHARS = MAX_LINE_CHARS * 2
MAX_CUE_SECONDS = 6.0
BREAK_GAP = 0.5
HOLD = 0.15


def srt_time(seconds: float) -> str:
    ms = round(max(0.0, seconds) * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _wrap_two_lines(text: str) -> str:
    if len(text) <= MAX_LINE_CHARS:
        return text
    words = text.split(" ")
    best, best_diff = 0, None
    for i in range(1, len(words)):
        left, right = " ".join(words[:i]), " ".join(words[i:])
        diff = abs(len(left) - len(right))
        if best_diff is None or diff < best_diff:
            best, best_diff = i, diff
    return " ".join(words[:best]) + "\n" + " ".join(words[best:])


def build_cues(
    analysis: ScriptAnalysis, words: Sequence[WordTiming]
) -> list[tuple[float, float, str]]:
    cues: list[list[WordTiming]] = []
    current: list[WordTiming] = []
    for w in words:
        if current:
            prev = current[-1]
            same_sentence = (
                analysis.tokens[w.token_idx].sentence_idx
                == analysis.tokens[prev.token_idx].sentence_idx
            )
            text_len = len(" ".join(x.text for x in current)) + 1 + len(w.text)
            too_long = text_len > MAX_CUE_CHARS or w.end - current[0].start > MAX_CUE_SECONDS
            if not same_sentence or w.start - prev.end > BREAK_GAP or too_long:
                cues.append(current)
                current = []
        current.append(w)
    if current:
        cues.append(current)

    out: list[tuple[float, float, str]] = []
    for i, cue in enumerate(cues):
        start = cue[0].start
        end = cue[-1].end + HOLD
        if i + 1 < len(cues):
            end = min(end, cues[i + 1][0].start)
        end = max(end, start + 0.2)
        out.append((start, end, _wrap_two_lines(" ".join(w.text for w in cue))))
    return out


def build_srt(analysis: ScriptAnalysis, words: Sequence[WordTiming]) -> str:
    blocks = []
    for n, (start, end, text) in enumerate(build_cues(analysis, words), start=1):
        blocks.append(f"{n}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n")
    return "\n".join(blocks)
