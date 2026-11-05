"""Exception hierarchy. Every message says what happened and what to do."""

from __future__ import annotations


class SnipmintError(Exception):
    """Base class for all expected Snipmint errors."""


class DependencyMissing(SnipmintError):
    """An optional extra (or external program) is not installed."""

    def __init__(self, what: str, hint: str):
        self.what = what
        self.hint = hint
        super().__init__(f"{what} is not installed. {hint}")


class FFmpegError(SnipmintError):
    """ffmpeg/ffprobe failed or is missing."""

    def __init__(self, message: str, stderr_tail: str = ""):
        self.stderr_tail = stderr_tail
        super().__init__(message + (f"\n--- ffmpeg output ---\n{stderr_tail}" if stderr_tail else ""))


class EngineError(SnipmintError):
    """A voice engine failed (network, model, bad voice id)."""


class EngineUnavailable(EngineError):
    """The engine cannot be used right now (missing extra, model not downloaded)."""


class ScriptError(SnipmintError):
    """The script (or a duration string) is empty or invalid."""


class ModelDownloadError(SnipmintError):
    """A model file could not be downloaded or failed verification."""


class JobCancelled(SnipmintError):
    """The user cancelled a running job."""


class IngestError(SnipmintError):
    """The video link is invalid, or the video could not be fetched."""


class AnalysisError(SnipmintError):
    """Transcription or moment analysis failed."""
