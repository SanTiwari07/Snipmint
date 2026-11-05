"""A source video on disk: its folder, metadata and per-stage status."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .. import config

StageName = Literal["probe", "download", "audio", "transcribe", "signals", "analyze"]
STAGES: tuple[StageName, ...] = ("probe", "download", "audio", "transcribe", "signals", "analyze")
StageState = Literal["pending", "running", "done", "failed", "skipped"]
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_locks: dict[str, threading.Lock] = {}


class Chapter(BaseModel):
    start: float
    end: float
    title: str


class HeatPoint(BaseModel):
    start: float
    end: float
    value: float


class SourceMeta(BaseModel):
    id: str
    url: str
    video_id: str | None = None
    title: str = ""
    channel: str = ""
    duration: float = 0.0
    width: int | None = None
    height: int | None = None
    thumbnail: str | None = None
    chapters: list[Chapter] = Field(default_factory=list)
    heatmap: list[HeatPoint] = Field(default_factory=list)
    language: str | None = None

    @property
    def vertical(self) -> bool:
        return bool(self.width and self.height and self.height > self.width)


class SourceStatus(BaseModel):
    stages: dict[str, StageState] = Field(default_factory=lambda: dict.fromkeys(STAGES, "pending"))
    error: str | None = None
    task_id: str | None = None
    warnings: list[str] = Field(default_factory=list)


class Source:
    """Folder layout: meta.json, status.json, source.mp4, audio.wav, transcript.json,
    signals.json, moments.<provider>.json."""

    def __init__(self, source_id: str, base: Path | None = None):
        if not _SAFE_ID.match(source_id):
            raise ValueError("bad source id")
        self.id = source_id
        self.path = (base or config.sources_dir()) / source_id
        self.path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def existing(cls, source_id: str, base: Path | None = None) -> Source | None:
        if not _SAFE_ID.match(source_id or ""):
            return None
        folder = (base or config.sources_dir()) / source_id
        return cls(source_id, base) if folder.is_dir() else None

    @property
    def lock(self) -> threading.Lock:
        return _locks.setdefault(self.id, threading.Lock())

    def file(self, name: str) -> Path:
        return self.path / name

    # ------------------------------------------------------------------ json helpers

    def read_json(self, name: str) -> Any:
        return json.loads(self.file(name).read_text(encoding="utf-8"))

    def write_json(self, name: str, data: Any) -> Path:
        path = self.file(name)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return path

    # ------------------------------------------------------------------ meta / status

    @property
    def meta(self) -> SourceMeta | None:
        f = self.file("meta.json")
        return SourceMeta.model_validate(self.read_json("meta.json")) if f.is_file() else None

    def save_meta(self, meta: SourceMeta) -> None:
        self.write_json("meta.json", meta.model_dump())

    @property
    def status(self) -> SourceStatus:
        f = self.file("status.json")
        if f.is_file():
            try:
                return SourceStatus.model_validate(self.read_json("status.json"))
            except ValueError:
                pass
        return SourceStatus()

    def save_status(self, status: SourceStatus) -> None:
        self.write_json("status.json", status.model_dump())

    def set_stage(self, stage: str, state: StageState, error: str | None = None) -> None:
        st = self.status
        st.stages[stage] = state
        if state == "failed":
            st.error = error
        elif state == "running":
            st.error = None
        self.save_status(st)

    # ------------------------------------------------------------------ files

    @property
    def video(self) -> Path:
        return self.file("source.mp4")

    @property
    def audio(self) -> Path:
        return self.file("audio.wav")

    def moments_file(self, provider: str) -> Path:
        return self.file(f"moments.{provider}.json")
