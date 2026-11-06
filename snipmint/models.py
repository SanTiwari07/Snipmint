"""Pydantic models shared by the core, CLI and HTTP API (see NarraReel WHAT_TO_DO.md section 4.4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class Token(BaseModel):
    text: str  # as written in the script, incl. punctuation: "Hello,"
    norm: str  # normalised for matching: "hello"
    start_char: int
    end_char: int
    sentence_idx: int
    emphasis: bool = False  # written as *word* in the script


class Sentence(BaseModel):
    idx: int
    text: str
    paragraph_idx: int
    token_start: int
    token_end: int  # exclusive
    ends_paragraph: bool = False


class ScriptWarning(BaseModel):
    code: str
    message: str
    sentence_idx: int | None = None


class ScriptAnalysis(BaseModel):
    text: str
    language_hint: str = "en"
    tokens: list[Token]
    sentences: list[Sentence]
    word_count: int
    char_count: int
    paragraph_count: int
    warnings: list[ScriptWarning] = Field(default_factory=list)


class VoiceInfo(BaseModel):
    engine: str
    id: str
    name: str
    language: str
    gender: Literal["female", "male", "neutral", "unknown"] = "unknown"
    accent: str | None = None
    offline: bool = False
    custom: bool = False
    sample_url: str | None = None

    @property
    def ref(self) -> str:
        return f"{self.engine}:{self.id}"


class VoiceSelection(BaseModel):
    engine: str
    voice_id: str
    speed: float = Field(1.0, ge=0.5, le=2.0)
    energy: float = Field(0.5, ge=0.0, le=1.0)

    @classmethod
    def parse(cls, ref: str, speed: float = 1.0, energy: float = 0.5) -> VoiceSelection:
        """``edge:en-US-JennyNeural`` -> VoiceSelection."""
        from .errors import ScriptError

        if ":" not in ref:
            raise ScriptError(
                f"Voice '{ref}' must look like <engine>:<voice>, e.g. edge:en-US-JennyNeural."
            )
        engine, voice_id = ref.split(":", 1)
        if not engine or not voice_id:
            raise ScriptError(f"Voice '{ref}' must look like <engine>:<voice>.")
        return cls(engine=engine, voice_id=voice_id, speed=speed, energy=energy)

    @property
    def ref(self) -> str:
        return f"{self.engine}:{self.voice_id}"


class DurationEstimate(BaseModel):
    speech_seconds: float
    pause_seconds: float
    total_seconds: float
    words_per_minute: float
    basis: Literal["calibrated", "default"] = "default"


Quality = Literal["natural", "noticeable", "strong", "not_recommended"]


class DurationOption(BaseModel):
    kind: Literal["edit_script", "adjust_delivery", "speed_only"]
    speed: float | None = None
    pause_scale: float | None = None
    words_delta: int | None = None
    resulting_seconds: float
    quality: Quality
    message: str


class DurationPlan(BaseModel):
    estimate_1x: DurationEstimate
    target_seconds: float | None = None
    options: list[DurationOption]
    recommended: int = 0


class WordTiming(BaseModel):
    token_idx: int
    text: str
    start: float
    end: float
    source: Literal["native", "aligned", "estimated"] = "estimated"


class SynthesisResult(BaseModel):
    audio_wav: Path
    audio_mp3: Path | None = None
    duration: float
    sample_rate: int
    words: list[WordTiming]
    sentence_spans: list[tuple[float, float]]
    target_seconds: float | None = None
    target_met: bool | None = None
    note: str | None = None
    speed: float = 1.0
    pause_scale: float = 1.0


class ProgressEvent(BaseModel):
    stage: Literal[
        "prepare", "voice", "timing", "captions", "render", "done", "error",
        "probe", "download", "audio", "transcribe", "signals", "analyze", "cut",
        "publish",
    ]
    fraction: float = 0.0
    message: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------- engines


class EngineCapabilities(BaseModel):
    offline: bool = False
    native_word_timing: bool = False
    native_speed: bool = False
    energy_control: Literal["none", "basic", "expressive"] = "none"
    cloning: bool = False
    languages: list[str] = Field(default_factory=list)
    needs_gpu: Literal["no", "recommended", "yes"] = "no"


class EngineStatus(BaseModel):
    available: bool
    reason: str | None = None
    needs_download: bool = False
    download_size_mb: int | None = None


class EngineInfo(BaseModel):
    id: str
    display_name: str
    capabilities: EngineCapabilities
    status: EngineStatus
