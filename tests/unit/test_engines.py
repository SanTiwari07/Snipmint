import hashlib
import http.server
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

from snipmint import download
from snipmint.engines import edge, kokoro, registry
from snipmint.errors import EngineError, EngineUnavailable, ModelDownloadError
from snipmint.models import VoiceSelection


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPMINT_HOME", str(tmp_path / "home"))
    registry.reset_instances()
    yield
    registry.reset_instances()


# ------------------------------------------------------------ registry / lazy


def test_importing_package_is_lazy():
    code = (
        "import sys, snipmint, snipmint.engines.registry as r; "
        "r.get_engine('edge'); r.get_engine('kokoro'); "
        "bad = [m for m in ('edge_tts', 'kokoro_onnx', 'faster_whisper', 'torch') if m in sys.modules]; "
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_unknown_engine():
    with pytest.raises(EngineError, match="Unknown voice engine"):
        registry.get_engine("nope")


def test_edge_is_available():
    infos = {i.id: i for i in registry.available_engines()}
    assert infos["edge"].status.available
    assert infos["edge"].capabilities.native_word_timing


def test_kokoro_unavailable_without_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "kokoro_onnx", None)  # makes `import kokoro_onnx` fail
    status = registry.get_engine("kokoro").status()
    assert not status.available
    assert 'pip install "snipmint[offline]"' in status.reason
    with pytest.raises(EngineUnavailable):
        registry.list_voices("kokoro")
    with pytest.raises(EngineUnavailable):
        registry.resolve_voice(VoiceSelection(engine="kokoro", voice_id="af_heart"))


def test_kokoro_needs_download_when_models_missing():
    pytest.importorskip("kokoro_onnx")
    status = registry.get_engine("kokoro").status()
    assert status.available and status.needs_download
    assert 100 < status.download_size_mb < 200


# ------------------------------------------------------------------- voices


def test_kokoro_voice_mapping():
    v = kokoro.voice_info_from_id("af_heart")
    assert (v.name, v.language, v.gender, v.accent, v.offline) == (
        "Heart (US, female)", "en-US", "female", "US", True,
    )
    v = kokoro.voice_info_from_id("bm_george")
    assert (v.name, v.language, v.gender, v.accent) == ("George (UK, male)", "en-GB", "male", "UK")
    assert kokoro.voice_info_from_id("hf_alpha").language == "hi-IN"
    # non-English voices keep the plain name until they get their own display names
    assert kokoro.voice_info_from_id("hf_alpha").name == "Alpha"
    assert kokoro.voice_info_from_id("zz") is None
    assert kokoro.voice_info_from_id("xx_unknown") is None
    assert kokoro.kokoro_lang("bf_emma") == "en-gb"


def test_kokoro_known_voices_all_map():
    assert all(kokoro.voice_info_from_id(v) for v in kokoro.KNOWN_VOICES)


def test_edge_curated_voices_first_and_filter(monkeypatch):
    eng = edge.EdgeEngine()
    monkeypatch.setattr(eng, "_fetch_all", lambda: [
        {"ShortName": "fr-FR-DeniseNeural", "Gender": "Female", "Locale": "fr-FR"},
        {"ShortName": "en-US-AvaNeural", "Gender": "Female", "Locale": "en-US"},
    ])
    voices = eng.list_voices()
    assert voices[0].id == "en-US-AvaNeural"
    assert sum(v.id == "en-US-AvaNeural" for v in voices) == 1
    assert [v.id for v in eng.list_voices("fr")] == ["fr-FR-DeniseNeural"]
    assert eng.get_voice("en-GB-RyanNeural").accent == "UK"
    assert all(v.language.lower().startswith("hi") for v in eng.list_voices("hi"))


def test_edge_voice_cache_roundtrip(monkeypatch):
    eng = edge.EdgeEngine()
    eng._cache_file().write_text(
        '{"fetched": %s, "voices": [{"ShortName": "de-DE-KatjaNeural", "Gender": "Female", '
        '"Locale": "de-DE"}]}'.replace("%s", str(time.time())),
        encoding="utf-8",
    )
    assert [v.id for v in eng.list_voices("de")] == ["de-DE-KatjaNeural"]


# --------------------------------------------------------------------- edge


def test_energy_to_prosody_ranges():
    assert edge.energy_to_prosody(0.0, 1.0) == ("-3%", "-4Hz", "-5%")
    assert edge.energy_to_prosody(1.0, 1.0) == ("+4%", "+6Hz", "+5%")
    rate, _, _ = edge.energy_to_prosody(0.5, 1.2)
    assert rate == "+20%"  # 20% from speed; nudge at energy 0.5 is only +0.5%


def _patch_stream(monkeypatch, eng, results):
    calls = []

    async def fake_stream(text, voice_id, rate, pitch, volume):
        calls.append((text, voice_id, rate, pitch, volume))
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(eng, "_stream", fake_stream)
    monkeypatch.setattr(
        edge, "decode_to_float32", lambda data, sr=48000: np.zeros(sr, dtype=np.float32)
    )
    return calls


def test_edge_synthesize_maps_words_to_seconds(monkeypatch):
    eng = edge.EdgeEngine()
    calls = _patch_stream(monkeypatch, eng, [(b"mp3", [("Hello", 0.1, 0.5), ("world", 0.5, 0.9)])])
    out = eng.synthesize("Hello world", VoiceSelection(engine="edge", voice_id="en-US-AvaNeural"))
    assert out.words == [("Hello", 0.1, 0.5), ("world", 0.5, 0.9)]
    assert out.sample_rate == 48000 and out.samples.size == 48000
    assert calls[0][1] == "en-US-AvaNeural"


def test_edge_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(edge, "RETRY_DELAYS", (0, 0, 0))
    eng = edge.EdgeEngine()
    calls = _patch_stream(monkeypatch, eng, [OSError("boom"), OSError("boom"), (b"mp3", [])])
    out = eng.synthesize("Hi.", VoiceSelection(engine="edge", voice_id="en-US-AvaNeural"))
    assert len(calls) == 3 and out.words is None


def test_edge_gives_up_with_friendly_error(monkeypatch):
    monkeypatch.setattr(edge, "RETRY_DELAYS", (0, 0))
    eng = edge.EdgeEngine()
    _patch_stream(monkeypatch, eng, [OSError("down")] * 3)
    with pytest.raises(EngineError, match="internet connection"):
        eng.synthesize("Hi.", VoiceSelection(engine="edge", voice_id="en-US-AvaNeural"))


def test_edge_bad_voice_is_not_retried(monkeypatch):
    eng = edge.EdgeEngine()
    calls = _patch_stream(monkeypatch, eng, [ValueError("Invalid voice"), (b"mp3", [])])
    with pytest.raises(EngineError, match="rejected"):
        eng.synthesize("Hi.", VoiceSelection(engine="edge", voice_id="bad"))
    assert len(calls) == 1


@pytest.mark.network
@pytest.mark.ffmpeg
def test_edge_live_boundaries_are_monotonic():
    eng = edge.EdgeEngine()
    out = eng.synthesize(
        "Hello world, this is a test.",
        VoiceSelection(engine="edge", voice_id="en-US-AvaNeural"),
    )
    assert out.words and len(out.words) == 6
    duration = out.samples.size / out.sample_rate
    starts = [w[1] for w in out.words]
    assert starts == sorted(starts)
    assert all(0 <= s < e <= duration + 0.2 for _, s, e in out.words)  # proves the 100 ns unit


# ----------------------------------------------------------------- download


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    payload = b""

    def log_message(self, *args):
        pass

    def do_GET(self):
        data = self.payload
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].rstrip("-"))
            if start >= len(data):
                self.send_response(416)
                self.end_headers()
                return
            body = data[start:]
            self.send_response(206)
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    payload = bytes(range(256)) * 4000  # ~1 MB
    handler = type("H", (_RangeHandler,), {"payload": payload})
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/f.bin", payload
    httpd.shutdown()


def test_download_verifies_and_reports_progress(server, tmp_path):
    url, payload = server
    seen = []
    dest = download.download_file(
        url, tmp_path / "m.bin", sha256=hashlib.sha256(payload).hexdigest(),
        progress=lambda f, m: seen.append(f),
    )
    assert dest.read_bytes() == payload
    assert seen and seen[-1] == pytest.approx(1.0)
    assert not (tmp_path / "m.bin.part").exists()
    # second call is a no-op
    assert download.download_file(url, dest, sha256=hashlib.sha256(payload).hexdigest()) == dest


def test_download_resumes_partial(server, tmp_path):
    url, payload = server
    (tmp_path / "m.bin.part").write_bytes(payload[:300_000])
    dest = download.download_file(url, tmp_path / "m.bin", sha256=hashlib.sha256(payload).hexdigest())
    assert dest.read_bytes() == payload


def test_download_detects_corruption(server, tmp_path):
    url, _ = server
    with pytest.raises(ModelDownloadError, match="integrity"):
        download.download_file(url, tmp_path / "m.bin", sha256="0" * 64)
    assert not (tmp_path / "m.bin").exists()


def test_download_unreachable(tmp_path):
    with pytest.raises(ModelDownloadError, match="internet connection"):
        download.download_file("http://127.0.0.1:9/nothing", tmp_path / "x.bin")
