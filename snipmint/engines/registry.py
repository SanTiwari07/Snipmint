"""Engine discovery with lazy imports (NarraReel NarraReel WHAT_TO_DO.md section 4.3, rule 2)."""

from __future__ import annotations

import importlib
import threading

from ..errors import EngineError, EngineUnavailable
from ..models import EngineInfo, VoiceInfo, VoiceSelection
from .base import TTSEngine

ENGINES: dict[str, str] = {
    "edge": "snipmint.engines.edge:EdgeEngine",
    "kokoro": "snipmint.engines.kokoro:KokoroEngine",
    "chatterbox": "snipmint.engines.chatterbox:ChatterboxEngine",
}

_instances: dict[str, TTSEngine] = {}
_lock = threading.Lock()


def get_engine(engine_id: str) -> TTSEngine:
    """Return the (cached) engine instance. Importing never needs the heavy extras."""
    with _lock:
        if engine_id in _instances:
            return _instances[engine_id]
        target = ENGINES.get(engine_id)
        if target is None:
            known = ", ".join(sorted(ENGINES))
            raise EngineError(f"Unknown voice engine '{engine_id}'. Available engines: {known}.")
        module_name, _, cls_name = target.partition(":")
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:  # engine module itself must import cleanly; extras load later
            raise EngineUnavailable(f"Engine '{engine_id}' could not be loaded: {exc}") from exc
        engine = getattr(module, cls_name)()
        _instances[engine_id] = engine
        return engine


def reset_instances() -> None:
    """Drop cached engine instances (tests)."""
    with _lock:
        _instances.clear()


def register_engine(engine_id: str, target: str) -> None:
    """Add or replace an engine (``module:Class``); used by plugins and tests."""
    with _lock:
        ENGINES[engine_id] = target
        _instances.pop(engine_id, None)


def available_engines() -> list[EngineInfo]:
    infos: list[EngineInfo] = []
    for engine_id in ENGINES:
        try:
            engine = get_engine(engine_id)
        except EngineError:
            continue
        infos.append(
            EngineInfo(
                id=engine.id,
                display_name=engine.display_name,
                capabilities=engine.capabilities,
                status=engine.status(),
            )
        )
    return infos


def list_voices(engine_id: str | None = None, language: str | None = None) -> list[VoiceInfo]:
    """Voices of one engine, or of every engine that is usable right now."""
    ids = [engine_id] if engine_id else list(ENGINES)
    voices: list[VoiceInfo] = []
    for eid in ids:
        try:
            engine = get_engine(eid)
        except EngineError:
            if engine_id:
                raise
            continue  # an engine that cannot even be imported is simply not offered
        if not engine.status().available:
            if engine_id:
                raise EngineUnavailable(engine.status().reason or f"{eid} is not available.")
            continue
        voices.extend(engine.list_voices(language))
    return voices


def resolve_voice(selection: VoiceSelection) -> tuple[TTSEngine, VoiceInfo | None]:
    engine = get_engine(selection.engine)
    status = engine.status()
    if not status.available:
        raise EngineUnavailable(status.reason or f"Engine '{selection.engine}' is not available.")
    return engine, engine.get_voice(selection.voice_id)
