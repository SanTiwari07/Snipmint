"""Synthetic transcripts and sources for the clip pipeline tests."""

from __future__ import annotations

from snipmint.transcribe.models import Segment, Transcript, Word

FILLER = "and then we talked about the plan for a while before moving on to other things today."


def make_transcript(
    sentences: list[str], word_dur: float = 0.3, gap: float = 0.25, start: float = 0.0
) -> Transcript:
    t = start
    segments = []
    for sent in sentences:
        words = []
        for raw in sent.split():
            words.append(Word(text=" " + raw, start=round(t, 3), end=round(t + word_dur, 3)))
            t += word_dur
        segments.append(Segment(start=words[0].start, end=words[-1].end, text=sent, words=words))
        t += gap
    return Transcript(language="en", duration=round(t, 3), segments=segments)


def long_transcript(n: int = 120, hook_at: int = 60) -> Transcript:
    """``n`` dull sentences with one punchy question planted at index ``hook_at``."""
    sentences = []
    for i in range(n):
        if i == hook_at:
            sentences.append("Why do you never see the biggest mistake that 9 out of 10 founders make?")
        else:
            sentences.append(f"So {FILLER}")
    return make_transcript(sentences)
