"""Per-run job folders (NarraReel NarraReel WHAT_TO_DO.md section 4.5): no shared temp files between runs."""

from __future__ import annotations

import contextlib
import json
import logging
import secrets
import shutil
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import config

log = logging.getLogger("snipmint.jobs")


class Workspace:
    """A unique folder for one run. Everything a job produces lives here."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._log_depth = 0

    @property
    def id(self) -> str:
        return self.path.name

    @classmethod
    def create(cls, base: Path | None = None) -> Workspace:
        base = Path(base) if base else config.jobs_dir()
        base.mkdir(parents=True, exist_ok=True)
        for _ in range(20):
            job_id = f"{datetime.now():%Y-%m-%d_%H%M%S}_{secrets.token_hex(3)}"
            path = base / job_id
            try:
                path.mkdir()
            except FileExistsError:
                continue
            return cls(path)
        raise RuntimeError("could not create a unique job folder")

    def file(self, name: str) -> Path:
        return self.path / name

    def write_json(self, name: str, data: Any) -> Path:
        path = self.file(name)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def read_json(self, name: str) -> Any:
        return json.loads(self.file(name).read_text(encoding="utf-8"))

    def write_project(self, project: dict[str, Any]) -> Path:
        return self.write_json("project.json", project)

    @contextlib.contextmanager
    def logging(self, level: int = logging.INFO) -> Iterator[None]:
        """Mirror ``snipmint.*`` log records into this job's ``log.txt`` (re-entrant)."""
        if self._log_depth:
            self._log_depth += 1
            try:
                yield
            finally:
                self._log_depth -= 1
            return
        self._log_depth = 1
        handler = logging.FileHandler(self.file("log.txt"), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler.setLevel(level)
        root = logging.getLogger("snipmint")
        old_level = root.level
        root.addHandler(handler)
        if root.level == logging.NOTSET or root.level > level:
            root.setLevel(level)
        try:
            yield
        finally:
            self._log_depth = 0
            root.removeHandler(handler)
            root.setLevel(old_level)
            handler.close()

    def __repr__(self) -> str:
        return f"Workspace({self.path})"


def list_jobs(base: Path | None = None) -> list[Path]:
    """Job folders, newest first."""
    base = Path(base) if base else config.jobs_dir()
    jobs = [p for p in base.iterdir() if p.is_dir()] if base.is_dir() else []
    return sorted(jobs, key=lambda p: p.name, reverse=True)


def clean_jobs(
    keep: int | None = None,
    max_age_days: float = 7.0,
    base: Path | None = None,
    now: float | None = None,
) -> list[Path]:
    """Delete jobs that are BOTH beyond the newest ``keep`` and older than ``max_age_days``."""
    keep = config.load_settings().keep_jobs if keep is None else keep
    now = time.time() if now is None else now
    removed: list[Path] = []
    for index, job in enumerate(list_jobs(base)):
        if index < keep:
            continue
        age_days = (now - job.stat().st_mtime) / 86400
        if age_days >= max_age_days:
            shutil.rmtree(job, ignore_errors=True)
            removed.append(job)
    return removed
