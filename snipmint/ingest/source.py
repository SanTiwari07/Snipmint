"""A source video on disk: its folder, metadata and per-stage status."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .. import config

StageName = Literal["probe", "download", "audio", "transcribe", "signals", "analyze"]
STAGES: tuple[StageName, ...] = ("probe", "download", "audio", "transcribe", "signals", "analyze")
StageState = Literal["pending", "running", "done", "failed", "skipped"]
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_locks: dict[str, threading.Lock] = {}
_io_locks: dict[str, threading.RLock] = {}


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

    @property
    def io_lock(self) -> threading.RLock:
        """Guards small JSON files (status, meta) that the API and the worker both touch."""
        return _io_locks.setdefault(self.id, threading.RLock())

    def read_json(self, name: str) -> Any:
        with self.io_lock:
            for attempt in range(20):
                try:
                    return json.loads(self.file(name).read_text(encoding="utf-8"))
                except PermissionError:  # Windows: another process is swapping the file in
                    if attempt == 19:
                        raise
                    time.sleep(0.025)

    def write_json(self, name: str, data: Any) -> Path:
        path = self.file(name)
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        with self.io_lock:
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            for attempt in range(20):
                try:
                    tmp.replace(path)
                    break
                except PermissionError:  # Windows: a reader (e.g. antivirus) has the file open
                    if attempt == 19:
                        tmp.unlink(missing_ok=True)
                        raise
                    time.sleep(0.025)
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

    def update_status(self, change: Callable[[SourceStatus], None]) -> SourceStatus:
        """Read-modify-write of status.json under the lock, so no update is lost."""
        with self.io_lock:
            st = self.status
            change(st)
            self.save_status(st)
            return st

    def set_stage(self, stage: str, state: StageState, error: str | None = None) -> None:
        def change(st: SourceStatus) -> None:
            st.stages[stage] = state
            if state == "failed":
                st.error = error
            elif state == "running":
                st.error = None

        self.update_status(change)

    # ------------------------------------------------------------------ files

    @property
    def video(self) -> Path:
        return self.file("source.mp4")

    @property
    def audio(self) -> Path:
        return self.file("audio.wav")

    def moments_file(self, provider: str) -> Path:
        return self.file(f"moments.{provider}.json")
