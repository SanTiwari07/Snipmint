"""Transcript data: words with times, grouped in segments."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Word(BaseModel):
    text: str
    start: float
    end: float
    prob: float = 1.0


class Segment(BaseModel):
    start: float
    end: float
    text: str
    words: list[Word] = Field(default_factory=list)


class Transcript(BaseModel):
    language: str = "unknown"
    duration: float = 0.0
    segments: list[Segment] = Field(default_factory=list)

    @property
    def words(self) -> list[Word]:
        return [w for s in self.segments for w in s.words]

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments).strip()

    def words_between(self, start: float, end: float) -> list[Word]:
        """Words whose middle falls inside [start, end)."""
        out = []
        for w in self.words:
            mid = (w.start + w.end) / 2
            if start <= mid < end:
                out.append(w)
        return out
