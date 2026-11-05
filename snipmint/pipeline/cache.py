"""On-disk cache of synthesized sentences, so re-runs don't call the engine again."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .. import config


@dataclass
class CachedClip:
    samples: np.ndarray  # float32, 48 kHz, silence-trimmed
    words: list[tuple[str, float, float]] | None  # engine words, relative to the UNtrimmed clip
    trim_offset: float


def make_key(
    engine: str, version: str, voice_id: str, speed: float, energy: float | None, text: str
) -> str:
    payload = json.dumps(
        [engine, version, voice_id, round(speed, 4), None if energy is None else round(energy, 4), text],
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SentenceCache:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else config.cache_dir() / "sentences"

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.npz"

    def get(self, key: str) -> CachedClip | None:
        path = self._path(key)
        try:
            with np.load(path, allow_pickle=False) as data:
                pcm = data["pcm"]
                meta = json.loads(str(data["meta"]))
        except (OSError, ValueError, KeyError):
            return None
        with contextlib.suppress(OSError):
            os.utime(path)  # keep recently used entries
        words = [tuple(w) for w in meta["words"]] if meta.get("words") else None
        return CachedClip(pcm.astype(np.float32) / 32767.0, words, float(meta["trim_offset"]))  # type: ignore[arg-type]

    def put(self, key: str, clip: CachedClip) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        pcm = (np.clip(clip.samples, -1.0, 1.0) * 32767.0).astype("<i2")
        meta = json.dumps({"words": clip.words, "trim_offset": clip.trim_offset})
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        try:
            with tmp.open("wb") as fh:
                np.savez(fh, pcm=pcm, meta=np.array(meta))
            tmp.replace(path)
        except OSError:
            tmp.unlink(missing_ok=True)  # a cache write failure must never fail a job

    def clean(self, max_age_days: float = 30.0) -> int:
        """Delete entries not used for ``max_age_days``. Returns how many were removed."""
        if not self.root.is_dir():
            return 0
        cutoff = time.time() - max_age_days * 86400
        removed = 0
        for f in self.root.glob("*/*.npz"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
