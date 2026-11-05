"""Timing quality checks (NarraReel NarraReel WHAT_TO_DO.md section 5.7.4)."""

from __future__ import annotations

from collections.abc import Sequence

from ..models import WordTiming

MIN_WORD_SECONDS = 0.06


def enforce_quality(
    words: list[WordTiming],
    limits: Sequence[tuple[float, float]] | None = None,
    min_duration: float = MIN_WORD_SECONDS,
) -> list[WordTiming]:
    """Make timings monotonic, give every word a minimum length, keep words inside ``limits``.

    ``limits[i]`` is the (start, end) of the sentence that word ``i`` belongs to, so a word can
    never cross a sentence boundary.
    """
    n = len(words)
    if n == 0:
        return words
    starts = [w.start for w in words]
    ends = [w.end for w in words]
    lo = [limits[i][0] if limits else float("-inf") for i in range(n)]
    hi = [limits[i][1] if limits else float("inf") for i in range(n)]

    # forward: no overlaps, minimum duration, stay inside the sentence start
    prev_end = float("-inf")
    for i in range(n):
        starts[i] = max(starts[i], prev_end, lo[i])
        ends[i] = max(ends[i], starts[i] + min_duration)
        prev_end = ends[i]

    # backward: pull words back if the minimum durations pushed them past their sentence end
    next_start = float("inf")
    for i in range(n - 1, -1, -1):
        ends[i] = min(ends[i], next_start, hi[i])
        starts[i] = min(starts[i], ends[i] - min_duration)
        if limits:
            starts[i] = max(starts[i], lo[i])
        starts[i] = max(starts[i], 0.0)
        next_start = starts[i]

    # a pulled-back start can only create a gap, never an overlap; make sure of order anyway
    for i in range(1, n):
        if starts[i] < ends[i - 1]:
            ends[i - 1] = starts[i]
    return [
        w.model_copy(update={"start": starts[i], "end": max(ends[i], starts[i])})
        for i, w in enumerate(words)
    ]
