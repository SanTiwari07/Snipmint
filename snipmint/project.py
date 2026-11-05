"""The project file: everything needed to (re)make a video (NarraReel NarraReel WHAT_TO_DO.md section 15.4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from . import __version__
from .errors import ScriptError
from .models import VoiceSelection


class DurationConfig(BaseModel):
    target_seconds: float | None = Field(None, gt=0)
    option: Literal["edit_script", "adjust_delivery", "speed_only"] | None = None
    speed: float | None = Field(None, ge=0.5, le=2.0)
    pause_scale: float | None = Field(None, gt=0)


class CaptionsConfig(BaseModel):
    preset: str = "bold-pop"
    overrides: dict[str, Any] = Field(default_factory=dict)
    font: str | None = None
    keep_emoji: bool = False


class BackgroundConfig(BaseModel):
    type: Literal["solid", "transparent", "image", "video"] = "solid"
    color: str = "#101010"
    path: str | None = None
    blur: float = Field(0.0, ge=0, le=60)
    darken: float = Field(0.0, ge=0, le=1)


class ExportConfig(BaseModel):
    video: Literal["mp4", "webm", "mov"] = "mp4"
    fps: int | None = Field(None, ge=10, le=60)
    loudness_lufs: float | None = Field(None, ge=-30, le=-6)
    quality: Literal["fast", "balanced", "high"] = "balanced"


class ProjectConfig(BaseModel):
    snipmint_version: str = __version__
    script: str
    duration: DurationConfig = Field(default_factory=DurationConfig)
    voice: VoiceSelection = Field(
        default_factory=lambda: VoiceSelection(engine="edge", voice_id="en-US-AvaNeural")
    )
    format: str = "reels"
    captions: CaptionsConfig = Field(default_factory=CaptionsConfig)
    background: BackgroundConfig = Field(default_factory=BackgroundConfig)
    export: ExportConfig = Field(default_factory=ExportConfig)

    @model_validator(mode="after")
    def _transparent_needs_alpha_container(self) -> ProjectConfig:
        if self.background.type == "transparent" and self.export.video == "mp4":
            self.export = self.export.model_copy(update={"video": "webm"})
        return self

    # ------------------------------------------------------------- files

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> ProjectConfig:
        path = Path(path)
        try:
            return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except OSError as exc:
            raise ScriptError(f"Could not open project file {path}: {exc}") from exc
        except ValueError as exc:
            raise ScriptError(f"{path.name} is not a valid Snipmint project: {exc}") from exc
