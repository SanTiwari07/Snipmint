import time

import numpy as np
import pytest

from snipmint.audio import ffmpeg as ff
from snipmint.audio import process as ap

SR = ap.SAMPLE_RATE

needs_ffmpeg = pytest.mark.skipif(not ff.available(), reason="ffmpeg not installed")


def sine(seconds, freq=220.0, amp=0.5, sr=SR):
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_make_silence_length():
    assert ap.make_silence(0.5).size == SR // 2


def test_trim_silence_keeps_pad_and_reports_offset():
    clip = np.concatenate([ap.make_silence(0.5), sine(0.4), ap.make_silence(0.7)])
    trimmed, offset = ap.trim_silence(clip)
    assert offset == pytest.approx(0.5 - 0.03, abs=0.015)
    assert trimmed.size / SR == pytest.approx(0.4 + 0.06, abs=0.03)


def test_trim_silence_all_silence():
    trimmed, offset = ap.trim_silence(ap.make_silence(1.0))
    assert trimmed.size < SR // 5 and offset == 0.0


def test_assemble_exact_positions():
    a, b = sine(0.5), sine(0.25)
    audio, spans = ap.assemble([a, b], [0.3], lead=0.2, tail=0.4)
    assert audio.size == round((0.2 + 0.5 + 0.3 + 0.25 + 0.4) * SR)
    assert spans[0] == pytest.approx((0.2, 0.7))
    assert spans[1] == pytest.approx((1.0, 1.25))
    assert np.allclose(audio[int(0.2 * SR) : int(0.7 * SR)], a)


def test_assemble_pause_count_validation():
    with pytest.raises(ValueError):
        ap.assemble([sine(0.1)], [0.1], 0, 0)


def test_assemble_ten_minutes_is_fast():
    clips = [sine(3.0) for _ in range(200)]
    t0 = time.perf_counter()
    audio, _ = ap.assemble(clips, [0.35] * 199, 0.25, 0.4)
    assert time.perf_counter() - t0 < 5
    assert audio.size / SR > 600


def test_tempo_filter_chains_large_factors():
    assert ap.tempo_filter(1.25) == "atempo=1.250000"
    assert ap.tempo_filter(3.0).count("atempo") == 2
    assert ap.tempo_filter(0.3).count("atempo") == 2
    with pytest.raises(ValueError):
        ap.tempo_filter(0)


def test_energy_filter_scales_with_energy():
    calm, hype = ap.energy_filter(0.0), ap.energy_filter(1.0)
    assert "ratio=1.50" in calm and "ratio=4.00" in hype
    assert "g=-1.00" in calm and "g=3.00" in hype
    assert calm.startswith("highpass=f=80")


def test_wav_roundtrip(tmp_path):
    x = sine(0.2)
    ap.write_wav(tmp_path / "a.wav", x)
    y, sr = ap.read_wav(tmp_path / "a.wav")
    assert sr == SR and y.size == x.size
    assert np.max(np.abs(x - y)) < 1e-3


def test_write_wav_clips_overload(tmp_path):
    ap.write_wav(tmp_path / "a.wav", np.array([2.0, -2.0], dtype=np.float32))
    y, _ = ap.read_wav(tmp_path / "a.wav")
    assert y.max() <= 1.0 and y.min() >= -1.0


# --- ffmpeg-backed ---------------------------------------------------------


@pytest.mark.ffmpeg
@needs_ffmpeg
def test_resample_length():
    out = ap.resample(sine(1.0, sr=24000), 24000, SR)
    assert out.size == pytest.approx(SR, rel=0.01)


@pytest.mark.ffmpeg
@needs_ffmpeg
def test_decode_mp3_roundtrip(tmp_path):
    ap.write_wav(tmp_path / "a.wav", sine(1.0))
    mp3 = ap.encode_mp3(tmp_path / "a.wav", tmp_path / "a.mp3")
    decoded = ap.decode_to_float32(mp3.read_bytes())
    assert decoded.size / SR == pytest.approx(1.0, abs=0.1)
    # piping skips the gapless header, so allow a few ms of encoder padding
    assert abs(ap.decode_to_float32(mp3).size - decoded.size) < SR // 20
    assert ff.probe_duration(mp3) == pytest.approx(1.0, abs=0.1)


@pytest.mark.ffmpeg
@needs_ffmpeg
def test_change_tempo_changes_length():
    x = sine(2.0)
    faster = ap.change_tempo(x, 2.0)
    slower = ap.change_tempo(x, 0.5)
    assert faster.size / SR == pytest.approx(1.0, abs=0.1)
    assert slower.size / SR == pytest.approx(4.0, abs=0.15)


@pytest.mark.ffmpeg
@needs_ffmpeg
def test_loudnorm_hits_target():
    rng = np.random.default_rng(0)
    envelope = 0.5 + 0.5 * np.sin(np.arange(10 * SR) / 9000)
    speechish = (sine(10.0, 200, 0.05) * envelope + rng.normal(0, 0.005, 10 * SR)).astype(
        np.float32
    )
    out = ap.loudnorm_two_pass(speechish, target=-14.0)
    assert ap.integrated_loudness(out) == pytest.approx(-14.0, abs=1.0)


@pytest.mark.ffmpeg
@needs_ffmpeg
def test_loudnorm_silence_passthrough():
    s = ap.make_silence(1.0)
    assert ap.loudnorm_two_pass(s).size == s.size


@pytest.mark.ffmpeg
@needs_ffmpeg
def test_apply_energy_keeps_length():
    x = sine(1.0)
    assert ap.apply_energy(x, 0.9).size == pytest.approx(x.size, abs=SR // 50)


@pytest.mark.ffmpeg
@needs_ffmpeg
def test_ffmpeg_error_has_stderr_tail():
    from snipmint.errors import FFmpegError

    with pytest.raises(FFmpegError) as exc:
        ff.run_ffmpeg(["-i", "does-not-exist.wav", "-f", "null", "-"])
    assert "does-not-exist" in str(exc.value)


@pytest.mark.ffmpeg
@needs_ffmpeg
def test_libass_present_on_this_machine():
    assert isinstance(ff.has_libass(), bool)
    assert ff.version() != "unknown"
