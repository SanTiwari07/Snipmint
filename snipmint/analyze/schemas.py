"""Shapes shared by every analysis provider: what goes in, what comes out."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Provider = Literal["heuristic", "local_llm", "gemini"]
ProviderChoice = Literal["auto", "heuristic", "local_llm", "gemini"]
Layout = Literal["center", "track", "split", "screencast"]

LENGTH_BANDS: dict[str, tuple[float, float]] = {
    "short": (15.0, 30.0),
    "medium": (30.0, 60.0),
    "long": (60.0, 90.0),
}


class AnalyzeOptions(BaseModel):
    provider: ProviderChoice = "auto"
    min_seconds: float = Field(30.0, ge=5, le=175)
    max_seconds: float = Field(60.0, ge=10, le=180)
    count: int | None = Field(None, ge=1, le=20)  # None = automatic from duration

    @model_validator(mode="after")
    def _band(self) -> AnalyzeOptions:
        if self.max_seconds < self.min_seconds + 5:
            self.max_seconds = min(180.0, self.min_seconds + 5)
        return self


class Moment(BaseModel):
    id: str
    start: float
    end: float
    score: int = Field(ge=0, le=100)
    why: str = ""
    title: str = ""
    hook_text: str = ""
    descriptions: dict[str, str] = Field(default_factory=dict)
    keywords: list[str] = Field(default_factory=list)
    provider: Provider = "heuristic"
    fallback: bool = False
    layout: Layout | None = None
    text: str = ""  # transcript of the clip (shown on the card)

    @property
    def duration(self) -> float:
        return self.end - self.start


class AnalysisResult(BaseModel):
    provider: Provider
    requested: ProviderChoice = "auto"
    fallback_reason: str | None = None
    options: AnalyzeOptions
    moments: list[Moment]
    cost_usd: float | None = None
