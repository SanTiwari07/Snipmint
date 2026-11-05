"""Paths and settings. Environment variables always win."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    tomllib = None  # type: ignore[assignment]

DEFAULT_PORT = 8000
DEFAULT_HOST = "127.0.0.1"


def home_dir() -> Path:
    """Data directory (``SNIPMINT_HOME`` or ``~/.snipmint``)."""
    env = os.environ.get("SNIPMINT_HOME")
    return Path(env).expanduser() if env else Path.home() / ".snipmint"


def _sub(name: str) -> Path:
    path = home_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def models_dir() -> Path:
    return _sub("models")


def voices_dir() -> Path:
    return _sub("voices")


def cache_dir() -> Path:
    return _sub("cache")


def jobs_dir() -> Path:
    return _sub("jobs")


def user_fonts_dir() -> Path:
    path = home_dir() / "fonts" / "user"
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_presets_dir() -> Path:
    path = home_dir() / "presets" / "user"
    path.mkdir(parents=True, exist_ok=True)
    return path


def package_dir() -> Path:
    return Path(__file__).resolve().parent


def assets_dir() -> Path:
    return package_dir() / "assets"


def presets_dir() -> Path:
    return package_dir() / "presets"


@dataclass(frozen=True)
class Settings:
    ffmpeg: str | None
    ffprobe: str | None
    device: str
    host: str
    port: int
    token: str | None
    keep_jobs: int
    loudness_lufs: float


def _file_config() -> dict:
    path = home_dir() / "config.toml"
    if tomllib is None or not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except Exception:  # a broken config file must never stop the app
        return {}


def load_settings() -> Settings:
    cfg = _file_config()

    def pick(env: str, key: str, default):
        value = os.environ.get(env)
        if value not in (None, ""):
            return value
        return cfg.get(key, default)

    return Settings(
        ffmpeg=pick("SNIPMINT_FFMPEG", "ffmpeg", None),
        ffprobe=pick("SNIPMINT_FFPROBE", "ffprobe", None),
        device=str(pick("SNIPMINT_DEVICE", "device", "auto")),
        host=str(pick("SNIPMINT_HOST", "host", DEFAULT_HOST)),
        port=int(pick("SNIPMINT_PORT", "port", DEFAULT_PORT)),
        token=pick("SNIPMINT_TOKEN", "token", None),
        keep_jobs=int(pick("SNIPMINT_KEEP_JOBS", "keep_jobs", 20)),
        loudness_lufs=float(pick("SNIPMINT_LUFS", "loudness_lufs", -14.0)),
    )
