import threading

import numpy as np
import pytest

from snipmint.audio import ffmpeg as ff
from snipmint.errors import EngineUnavailable, JobCancelled, ScriptError
from snipmint.jobs.workspace import Workspace
from snipmint.models import EngineStatus, VoiceSelection
from snipmint.pipeline.cache import SentenceCache
from snipmint.pipeline.speak import preview_audio, speak
from snipmint.script import analyze_script, format_duration

from .fake_engine import FakeEngine

pytestmark = [
    pytest.mark.ffmpeg,
    pytest.mark.skipif(not ff.available(), reason="ffmpeg not installed"),
]

SR = 48000

VOICE = VoiceSelection(engine="fake", voice_id="v1")


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPMINT_HOME", str(tmp_path / "home"))


@pytest.fixture
def ws(tmp_path):
    return Workspace.create(tmp_path / "jobs")


SCRIPT = (
    "Wake up early. Grind hard every single day. Success is waiting for you at the end of the "
    "road, so keep going.\n\nNobody is coming to save you. That is the good news! "
    "Build your own luck, one small step at a time."
)


def test_basic_speak_outputs(ws):
    eng = FakeEngine()
    res = speak(SCRIPT, VOICE, ws, engine=eng, cache=False)
    a = analyze_script(SCRIPT)
    assert res.audio_wav.is_file() and res.audio_mp3.is_file()
    assert (ws.path / "words.json").is_file() and (ws.path / "captions.srt").is_file()
    assert (ws.path / "project.json").is_file()
    assert len(res.words) == a.word_count
    assert [w.token_idx for w in res.words] == list(range(a.word_count))
    assert all(w.source == "native" for w in res.words)
    for x, y in zip(res.words, res.words[1:], strict=False):
        assert x.start < x.end <= y.start + 1e-9
    assert res.words[-1].end <= res.duration
    assert len(res.sentence_spans) == len(a.sentences)
    assert eng.calls == len(a.sentences)
    assert "-->" in (ws.path / "captions.srt").read_text(encoding="utf-8")


def test_word_timings_match_where_the_audio_is_loud(ws):
    """The fake engine's tone starts exactly where the first word is reported."""
    from snipmint.audio.process import read_wav

    res = speak("Hello there my friend.", VOICE, ws, engine=FakeEngine(), cache=False,
                energy_processing=False)
    audio, sr = read_wav(res.audio_wav)
    loud = np.nonzero(np.abs(audio) > 0.02)[0]
    first_sound = loud[0] / sr
    assert res.words[0].start == pytest.approx(first_sound, abs=0.06)
    last_sound = loud[-1] / sr
    assert res.words[-1].end == pytest.approx(last_sound, abs=0.12)


def test_estimated_timings_for_engines_without_native_words(ws):
    res = speak(SCRIPT, VOICE, ws, engine=FakeEngine(native_words=False), cache=False,
                timing="estimated")
    assert all(w.source == "estimated" for w in res.words)
    for x, y in zip(res.words, res.words[1:], strict=False):
        assert x.end <= y.start + 1e-9


@pytest.mark.parametrize("target", [20.0, 25.0, 30.0])
def test_target_duration_within_two_percent(ws, target):
    long_script = " ".join([SCRIPT] * 2)
    res = speak(long_script, VOICE, ws, engine=FakeEngine(), cache=False, target_seconds=target)
    assert res.target_met, res.note
    assert res.duration == pytest.approx(target, rel=0.02)


def test_impossible_target_reports_note(ws):
    res = speak(SCRIPT, VOICE, ws, engine=FakeEngine(), cache=False, target_seconds=5)
    assert res.target_met is False
    assert "Cut about" in res.note
    assert "0:05" in res.note
    assert res.duration > 5


def test_target_far_above_reports_add_words(ws):
    res = speak("Short script here.", VOICE, ws, engine=FakeEngine(), cache=False,
                target_seconds=60)
    assert res.target_met is False
    assert "Add about" in res.note


def test_retarget_reuses_cached_sentences(ws, tmp_path):
    cache = SentenceCache(tmp_path / "cache")
    eng = FakeEngine()
    a = analyze_script(SCRIPT)
    first = speak(SCRIPT, VOICE, ws, engine=eng, cache=cache, pause_scale=1.0)
    assert eng.calls == len(a.sentences)
    # a different pause scale changes only the silences -> no new synthesis
    ws2 = Workspace.create(tmp_path / "jobs")
    second = speak(SCRIPT, VOICE, ws2, engine=eng, cache=cache, pause_scale=2.0)
    assert eng.calls == len(a.sentences)
    assert second.duration > first.duration
    assert ws.id != ws2.id


def test_target_in_pause_range_keeps_speed_and_cache(ws, tmp_path):
    cache = SentenceCache(tmp_path / "cache")
    eng = FakeEngine()
    base = speak(SCRIPT, VOICE, ws, engine=eng, cache=cache)
    calls = eng.calls
    ws2 = Workspace.create(tmp_path / "jobs")
    res = speak(SCRIPT, VOICE, ws2, engine=eng, cache=cache, target_seconds=base.duration + 2)
    assert res.speed == pytest.approx(1.0, abs=0.01)
    assert eng.calls == calls  # pauses only: nothing re-synthesized
    assert res.duration == pytest.approx(base.duration + 2, rel=0.02)


def test_non_native_speed_uses_tempo(ws):
    eng = FakeEngine(native_speed=False)
    fast = speak(SCRIPT, VoiceSelection(engine="fake", voice_id="v1", speed=1.5), ws, engine=eng,
                 cache=False, pause_scale=1.0)
    ws2 = Workspace.create(ws.path.parent)
    normal = speak(SCRIPT, VOICE, ws2, engine=FakeEngine(native_speed=False), cache=False,
                   pause_scale=1.0)
    assert fast.duration < normal.duration * 0.85


def test_empty_script_errors(ws):
    with pytest.raises(ScriptError, match="empty"):
        speak("   ", VOICE, ws, engine=FakeEngine(), cache=False)


def test_unavailable_engine(ws):
    class Down(FakeEngine):
        def status(self):
            return EngineStatus(available=False, reason="not installed")

    with pytest.raises(EngineUnavailable, match="not installed"):
        speak("Hi there.", VOICE, ws, engine=Down(), cache=False)


def test_needs_download_gives_hint(ws):
    class NeedsModel(FakeEngine):
        def status(self):
            return EngineStatus(available=True, needs_download=True, download_size_mb=114,
                                reason="Model missing.")

    with pytest.raises(EngineUnavailable, match=r"snipmint prepare fake"):
        speak("Hi there.", VOICE, ws, engine=NeedsModel(), cache=False)


def test_cancel_before_start(ws):
    ev = threading.Event()
    ev.set()
    with pytest.raises(JobCancelled):
        speak(SCRIPT, VOICE, ws, engine=FakeEngine(), cache=False, cancel=ev)


def test_progress_events(ws):
    events = []
    speak(SCRIPT, VOICE, ws, engine=FakeEngine(), cache=False, progress=events.append)
    stages = [e.stage for e in events]
    assert stages[0] == "prepare" and stages[-1] == "done"
    assert "voice" in stages and "timing" in stages
    fractions = [e.fraction for e in events]
    assert fractions[-1] == 1.0 and max(fractions) <= 1.0


def test_long_sentence_is_split_into_units(ws):
    words = ["alpha,"] * 4 + ["beta"] * 70
    text = " ".join(words) + "."
    eng = FakeEngine()
    res = speak(text, VOICE, ws, engine=eng, cache=False)
    assert eng.calls >= 2
    assert len(res.words) == 74
    assert len(res.sentence_spans) == 1


def test_loudness_is_normalised(ws):
    from snipmint.audio.process import decode_to_float32, integrated_loudness

    res = speak(SCRIPT, VOICE, ws, engine=FakeEngine(), cache=False, loudness=-16.0)
    lufs = integrated_loudness(decode_to_float32(res.audio_wav))
    assert lufs == pytest.approx(-16.0, abs=1.5)


def test_two_runs_do_not_share_files(tmp_path):
    a, b = Workspace.create(tmp_path), Workspace.create(tmp_path)
    assert a.path != b.path
    speak("One thing.", VOICE, a, engine=FakeEngine(), cache=False)
    assert not (b.path / "voice.wav").exists()


def test_preview_audio_is_short(ws):
    audio = preview_audio(SCRIPT, VOICE, max_sentences=2, engine=FakeEngine(), cache=False)
    full = speak(SCRIPT, VOICE, ws, engine=FakeEngine(), cache=False)
    assert 0 < audio.size / SR < full.duration / 2


def test_format_duration_note_matches_result(ws):
    res = speak(SCRIPT, VOICE, ws, engine=FakeEngine(), cache=False, target_seconds=5)
    assert format_duration(res.duration) in res.note
