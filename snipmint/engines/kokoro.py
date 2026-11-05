"""Offline voices: Kokoro-82M through ``kokoro-onnx`` (NarraReel NarraReel WHAT_TO_DO.md section 5.3.3).

Optional extra: ``pip install "snipmint[offline]"``. The weights are Apache-2.0 and are
downloaded on first use (never bundled). ``kokoro-onnx`` pulls in GPL-licensed phonemizer
components, which is why this engine is an optional extra (see THIRD_PARTY_LICENSES.md).
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass

from .. import config
from ..audio.process import SAMPLE_RATE  # noqa: F401  (re-exported for callers)
from ..download import download_file
from ..errors import EngineError, EngineUnavailable
from ..models import EngineCapabilities, EngineStatus, VoiceInfo, VoiceSelection
from .base import ProgressFn, SentenceAudio, TTSEngine

log = logging.getLogger("snipmint.engines.kokoro")

INSTALL_HINT = 'Install with: pip install "snipmint[offline]"'
_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1"


@dataclass(frozen=True)
class ModelFile:
    name: str
    url: str
    sha256: str
    size: int


MODEL_VARIANTS: dict[str, ModelFile] = {
    "int8": ModelFile(
        "kokoro-v1.0.int8.onnx",
        f"{_RELEASE}/kokoro-v1.0.int8.onnx",
        "ae315a79b623f244700e4afb9246c46a26066782e049ba174bf3ba433970ee9c",
        114_119_327,
    ),
    "fp16": ModelFile(
        "kokoro-v1.0.fp16.onnx",
        f"{_RELEASE}/kokoro-v1.0.fp16.onnx",
        "f3a290d384fbb27966d462905c71a46cef9e5fd00516b40df32a0b4afe77ac96",
        163_527_961,
    ),
    "fp32": ModelFile(
        "kokoro-v1.0.onnx",
        f"{_RELEASE}/kokoro-v1.0.onnx",
        "beb0d1848dee9a49da392cc3df26958d46cfa35d321edf434f52949153f0df3a",
        325_505_369,
    ),
}
VOICES_FILE = ModelFile(
    "voices-v1.0.bin",
    f"{_RELEASE}/voices-v1.0.bin",
    "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
    28_214_398,
)
DEFAULT_VARIANT = "int8"

# first letter of a Kokoro voice id -> (BCP-47 language, kokoro `lang` argument, accent)
_LANGS: dict[str, tuple[str, str, str]] = {
    "a": ("en-US", "en-us", "US"),
    "b": ("en-GB", "en-gb", "UK"),
    "h": ("hi-IN", "hi", "IN"),
    "e": ("es-ES", "es", "ES"),
    "f": ("fr-FR", "fr-fr", "FR"),
    "i": ("it-IT", "it", "IT"),
    "p": ("pt-BR", "pt-br", "BR"),
}

KNOWN_VOICES = [
    "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky", "af_nova", "af_alloy",
    "af_aoede", "af_jessica", "af_kore", "af_river",
    "am_adam", "am_michael", "am_eric", "am_liam", "am_echo", "am_fenrir", "am_onyx",
    "am_puck", "am_santa",
    "bf_emma", "bf_isabella", "bf_alice", "bf_lily",
    "bm_george", "bm_lewis", "bm_daniel", "bm_fable",
    "hf_alpha", "hf_beta", "hm_omega", "hm_psi",
    "ef_dora", "em_alex", "ff_siwis", "if_sara", "im_nicola", "pf_dora", "pm_alex",
]  # fmt: skip


def voice_info_from_id(voice_id: str) -> VoiceInfo | None:
    """``af_heart`` -> Heart (US, female). None for unsupported languages."""
    if len(voice_id) < 4 or voice_id[2] != "_":
        return None
    lang = _LANGS.get(voice_id[0])
    if lang is None:
        return None
    gender = {"f": "female", "m": "male"}.get(voice_id[1], "unknown")
    name = voice_id[3:].replace("_", " ").title()
    if lang[0].startswith("en-"):  # English voices get "Heart (US, female)" in the UI and CLI
        name = f"{name} ({lang[2]}, {gender})"
    return VoiceInfo(
        engine="kokoro",
        id=voice_id,
        name=name,
        language=lang[0],
        gender=gender,  # type: ignore[arg-type]
        accent=lang[2],
        offline=True,
    )


def kokoro_lang(voice_id: str) -> str:
    return _LANGS.get(voice_id[:1], _LANGS["a"])[1]


def model_dir():
    path = config.models_dir() / "kokoro"
    path.mkdir(parents=True, exist_ok=True)
    return path


def variant() -> str:
    v = os.environ.get("SNIPMINT_KOKORO_MODEL", DEFAULT_VARIANT).lower()
    return v if v in MODEL_VARIANTS else DEFAULT_VARIANT


def model_path():
    return model_dir() / MODEL_VARIANTS[variant()].name


def voices_path():
    return model_dir() / VOICES_FILE.name


def models_present() -> bool:
    return model_path().is_file() and voices_path().is_file()


def download_size_mb() -> int:
    return round((MODEL_VARIANTS[variant()].size + VOICES_FILE.size) / 1_000_000)


class KokoroEngine(TTSEngine):
    id = "kokoro"
    display_name = "Offline voices (Kokoro)"
    version = "1"
    capabilities = EngineCapabilities(
        offline=True,
        native_word_timing=False,
        native_speed=True,
        energy_control="none",
        cloning=False,
        languages=["en", "hi"],
        needs_gpu="no",
    )

    def __init__(self) -> None:
        self._kokoro = None
        self._lock = threading.Lock()

    # -------------------------------------------------------------- status

    def status(self) -> EngineStatus:
        try:
            import kokoro_onnx  # noqa: F401
        except ImportError:
            return EngineStatus(available=False, reason=INSTALL_HINT)
        except Exception as exc:  # broken onnxruntime / espeak install
            return EngineStatus(available=False, reason=f"Kokoro could not start: {exc}")
        if not models_present():
            return EngineStatus(
                available=True,
                needs_download=True,
                download_size_mb=download_size_mb(),
                reason="The offline voice model hasn't been downloaded yet.",
            )
        return EngineStatus(available=True)

    # -------------------------------------------------------------- voices

    def list_voices(self, language: str | None = None) -> list[VoiceInfo]:
        ids = list(KNOWN_VOICES)
        if self._kokoro is not None:
            ids = [v for v in self._kokoro.get_voices() if voice_info_from_id(v)] or ids
        voices = [v for v in map(voice_info_from_id, ids) if v is not None]
        if language:
            prefix = language.lower()
            voices = [v for v in voices if v.language.lower().startswith(prefix)]
        return voices

    # ------------------------------------------------------------- models

    def prepare(self, progress: ProgressFn | None = None) -> None:
        """Download the model files (if missing) and load them."""
        mv = MODEL_VARIANTS[variant()]
        total = mv.size + VOICES_FILE.size

        def scaled(offset: int, size: int):
            def fn(fraction: float, message: str) -> None:
                if progress:
                    progress((offset + fraction * size) / total, message)

            return fn

        download_file(
            mv.url, model_path(), sha256=mv.sha256, expected_size=mv.size,
            progress=scaled(0, mv.size), label="offline voice model",
        )  # fmt: skip
        download_file(
            VOICES_FILE.url, voices_path(), sha256=VOICES_FILE.sha256,
            expected_size=VOICES_FILE.size, progress=scaled(mv.size, VOICES_FILE.size),
            label="offline voice list",
        )  # fmt: skip
        self._load()

    def _load(self):
        with self._lock:
            if self._kokoro is not None:
                return self._kokoro
            if not models_present():
                raise EngineUnavailable(
                    "The offline voice model hasn't been downloaded yet "
                    f"(~{download_size_mb()} MB). Run `snipmint prepare kokoro` first."
                )
            try:
                from kokoro_onnx import Kokoro
            except ImportError as exc:
                raise EngineUnavailable(INSTALL_HINT) from exc
            try:
                self._kokoro = Kokoro(str(model_path()), str(voices_path()))
            except Exception as exc:
                raise EngineError(
                    f"The offline voice model could not be loaded ({exc}). "
                    f"Delete {model_dir()} and run `snipmint prepare kokoro` again."
                ) from exc
            return self._kokoro

    # ----------------------------------------------------------- synthesis

    def synthesize(self, text: str, voice: VoiceSelection) -> SentenceAudio:
        kokoro = self._load()
        if voice.voice_id not in kokoro.get_voices():
            raise EngineError(f"Unknown offline voice '{voice.voice_id}'. Run `snipmint voices`.")
        try:
            with self._lock:  # one onnx session; keep calls serial
                samples, sr = kokoro.create(
                    text, voice=voice.voice_id, speed=float(voice.speed),
                    lang=kokoro_lang(voice.voice_id),
                )  # fmt: skip
        except Exception as exc:
            raise EngineError(f"Offline voice failed on '{text[:40]}...': {exc}") from exc
        return SentenceAudio(samples.astype("float32"), int(sr), None)
