"""Sentences, candidate clip spans, clip counts and word snapping.

``clip_count_targets``, ``shortlist_target`` and ``snap_to_words`` are adapted from
openshorts/clip_selection.py (MIT, (c) 2024 OpenShorts).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..transcribe.models import Transcript, Word

SENTENCE_END = (".", "?", "!", "…", "।", "?\"", "!\"", ".\"")
SENTENCE_GAP = 0.7  # seconds of silence that also ends a sentence
MAX_SENTENCE_WORDS = 40  # long run-ons are split so spans can still start inside them


@dataclass
class Sentence:
    idx: int
    first: int  # index of the first word in the flat word list
    last: int  # index of the last word (inclusive)
    start: float
    end: float
    text: str


def split_sentences(words: list[Word]) -> list[Sentence]:
    """Group words into sentences by end punctuation or a pause."""
    out: list[Sentence] = []
    begin = 0
    for i, w in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else None
        ends = w.text.rstrip().endswith(SENTENCE_END)
        gap = (nxt.start - w.end) if nxt else 0.0
        too_long = i - begin + 1 >= MAX_SENTENCE_WORDS
        if nxt is None or ends or gap >= SENTENCE_GAP or too_long:
            chunk = words[begin : i + 1]
            out.append(Sentence(
                idx=len(out), first=begin, last=i, start=chunk[0].start, end=chunk[-1].end,
                text=" ".join(x.text.strip() for x in chunk),
            ))  # fmt: skip
            begin = i + 1
    return out


@dataclass
class Span:
    first_sentence: int
    last_sentence: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def candidate_spans(
    sentences: list[Sentence], min_s: float, max_s: float, limit: int = 4000
) -> list[Span]:
    """Every run of whole sentences whose length lies in [min_s, max_s]."""
    spans: list[Span] = []
    n = len(sentences)
    for i in range(n):
        start = sentences[i].start
        for j in range(i, n):
            dur = sentences[j].end - start
            if dur > max_s:
                break
            if dur >= min_s:
                spans.append(Span(i, j, start, sentences[j].end))
    if len(spans) > limit:  # very long videos: thin evenly so coverage stays uniform
        step = len(spans) / limit
        spans = [spans[int(k * step)] for k in range(limit)]
    return spans


def shortlist_target(video_duration: float) -> int:
    """How many strong candidates a video of this length should surface (3-10)."""
    seconds = float(video_duration or 0)
    return max(3, min(10, int(seconds // 90) + 2))


def clip_count_targets(n_windows: int) -> tuple[int, int]:
    """(minimum, maximum) clips to return for ``n_windows`` shortlisted candidates."""
    n = max(1, int(n_windows or 1))
    low = max(2, min(6, n // 2 + 2))
    high = min(12, max(4, n * 2))
    return min(low, high), high


def auto_clip_count(video_duration: float) -> int:
    """Default number of clips: more for longer videos, 3 to 10."""
    low, high = clip_count_targets(shortlist_target(video_duration))
    minutes = float(video_duration or 0) / 60
    return max(low, min(high, 3 + int(minutes // 8), 10))


def snap_to_words(
    start: float, end: float, words: list[Word], video_duration: float,
    max_lead: float = 0.35, max_tail: float = 0.45,
) -> tuple[float, float]:
    """Move cut points into the silence around the nearest word boundaries."""
    if not words:
        return round(start, 3), round(end, 3)
    starts = [w.start for w in words]
    ends = [w.end for w in words]

    first = min(range(len(starts)), key=lambda k: abs(starts[k] - start))
    gap_before = starts[first] - ends[first - 1] if first > 0 else max_lead * 2
    new_start = max(0.0, starts[first] - min(max_lead, max(0.0, gap_before) / 2))

    last = min(range(len(ends)), key=lambda k: abs(ends[k] - end))
    gap_after = starts[last + 1] - ends[last] if last + 1 < len(words) else max_tail * 2
    new_end = min(float(video_duration or ends[last] + max_tail), ends[last] + min(max_tail, max(0.0, gap_after) / 2))
    if new_end <= new_start:
        return round(start, 3), round(end, 3)
    return round(new_start, 3), round(new_end, 3)


def overlap_ratio(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Overlap of two intervals relative to the shorter one (0..1)."""
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    shorter = max(1e-6, min(a[1] - a[0], b[1] - b[0]))
    return inter / shorter


def transcript_sentences(transcript: Transcript) -> tuple[list[Word], list[Sentence]]:
    words = transcript.words
    return words, split_sentences(words)
