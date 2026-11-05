"""Face detection with OpenCV's YuNet model (multi-face, with landmarks).

The model (~230 KB, MIT/Apache-2.0, from the OpenCV model zoo) downloads on first use into the
models folder and is verified by SHA-256. Without OpenCV or the model, callers fall back to a
centre crop.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .. import config
from ..download import download_file
from ..errors import ModelDownloadError

log = logging.getLogger("snipmint.reframe.faces")

YUNET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
    "face_detection_yunet_2023mar.onnx"
)
YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
MIN_SCORE = 0.6
_lock = threading.Lock()


@dataclass
class Face:
    x: float
    y: float
    w: float
    h: float
    score: float
    mouth: tuple[float, float, float, float] | None = None  # right corner x,y, left corner x,y

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def area(self) -> float:
        return self.w * self.h

    def scaled(self, k: float, dx: float = 0.0, dy: float = 0.0) -> Face:
        m = self.mouth
        return Face(
            self.x * k + dx, self.y * k + dy, self.w * k, self.h * k, self.score,
            (m[0] * k + dx, m[1] * k + dy, m[2] * k + dx, m[3] * k + dy) if m else None,
        )  # fmt: skip


def model_path() -> Path:
    return config.models_dir() / "yunet" / "face_detection_yunet_2023mar.onnx"


def ensure_model() -> Path | None:
    """The YuNet model file, downloading it once. ``None`` when it can't be had."""
    path = model_path()
    try:
        return download_file(YUNET_URL, path, sha256=YUNET_SHA256, label="face detection model")
    except ModelDownloadError as exc:
        log.warning("Face detection unavailable: %s", exc)
        return None


def available() -> bool:
    try:
        import cv2

        return hasattr(cv2, "FaceDetectorYN")
    except Exception:
        return False


class FaceDetector:
    """Detects faces in BGR frames of one fixed size."""

    def __init__(self, width: int, height: int):
        import cv2

        path = ensure_model()
        if path is None:
            raise RuntimeError("face detection model not available")
        with _lock:
            self._det = cv2.FaceDetectorYN.create(str(path), "", (width, height), MIN_SCORE, 0.3, 50)
        self.size = (width, height)

    def detect(self, frame: np.ndarray) -> list[Face]:
        h, w = frame.shape[:2]
        if (w, h) != self.size:
            self._det.setInputSize((w, h))
            self.size = (w, h)
        _ok, faces = self._det.detect(frame)
        out: list[Face] = []
        if faces is None:
            return out
        for f in faces:
            x, y, fw, fh = (float(v) for v in f[:4])
            score = float(f[14])
            if score < MIN_SCORE or fw < 8 or fh < 8:
                continue
            mouth = (float(f[10]), float(f[11]), float(f[12]), float(f[13]))
            out.append(Face(x, y, fw, fh, score, mouth))
        return out
