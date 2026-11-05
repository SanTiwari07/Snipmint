import json

import pytest
from typer.testing import CliRunner

from snipmint import __version__
from snipmint.audio import ffmpeg as ff
from snipmint.cli import app

runner = CliRunner()

SCRIPT = "Wake up early. Grind hard every day.\n\nSuccess is waiting for you at the end."


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPMINT_HOME", str(tmp_path / "home"))


@pytest.fixture
def script_file(tmp_path):
    p = tmp_path / "script.txt"
    p.write_text(SCRIPT, encoding="utf-8")
    return p


def test_help_lists_commands():
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    for cmd in ("doctor", "voices", "analyze", "speak", "prepare", "clean"):
        assert cmd in res.output


@pytest.mark.parametrize("cmd", ["doctor", "voices", "analyze", "speak", "prepare", "clean"])
def test_each_command_has_help(cmd):
    res = runner.invoke(app, [cmd, "--help"])
    assert res.exit_code == 0 and "Usage" in res.output


def test_version():
    res = runner.invoke(app, ["--version"])
    assert res.exit_code == 0 and __version__ in res.output


@pytest.mark.parametrize("json_out", [False, True])
@pytest.mark.parametrize("ffmpeg_missing", [False, True])
def test_doctor_reports_version_first(monkeypatch, json_out, ffmpeg_missing):
    from snipmint.engines import registry
    from snipmint.errors import DependencyMissing

    def find_ffmpeg():
        if ffmpeg_missing:
            raise DependencyMissing("ffmpeg", "Install ffmpeg.")
        return "ffmpeg"

    monkeypatch.setattr(ff, "find_ffmpeg", find_ffmpeg)
    monkeypatch.setattr(ff, "version", lambda: "7.0")
    monkeypatch.setattr(ff, "find_ffprobe", lambda: "ffprobe")
    monkeypatch.setattr(ff, "has_libass", lambda: True)
    monkeypatch.setattr(registry, "available_engines", lambda: [])

    res = runner.invoke(app, ["doctor", "--json"] if json_out else ["doctor"])
    assert res.exit_code == (1 if ffmpeg_missing else 0), res.output
    if json_out:
        checks = json.loads(res.output)
        assert checks[0] == {"name": "Snipmint", "ok": True, "detail": __version__, "core": True}
        assert checks[1]["name"] == "Python"
    else:
        assert res.output.splitlines()[0] == f"[OK  ] Snipmint: {__version__}"
        assert "Python:" in res.output
        assert "ffmpeg:" in res.output


def test_analyze_text(script_file):
    res = runner.invoke(app, ["analyze", str(script_file), "--target", "30s"])
    assert res.exit_code == 0, res.output
    assert "words" in res.output and "adjust delivery" in res.output


def test_analyze_json(script_file):
    res = runner.invoke(app, ["analyze", str(script_file), "--json", "--target", "1m"])
    data = json.loads(res.output)
    assert data["words"] == 15 and data["paragraphs"] == 2
    assert data["plan"]["options"]


def test_analyze_missing_file_is_friendly():
    res = runner.invoke(app, ["analyze", "nope.txt"])
    assert res.exit_code == 1
    assert "not found" in res.output


def test_analyze_bad_target(script_file):
    res = runner.invoke(app, ["analyze", str(script_file), "--target", "soon"])
    assert res.exit_code == 1 and "not a valid duration" in res.output


def test_speak_rejects_speed_and_target(script_file):
    res = runner.invoke(app, ["speak", str(script_file), "--speed", "1.2", "--target", "30s"])
    assert res.exit_code == 1 and "either --speed or --target" in res.output


def test_speak_bad_voice_reference(script_file):
    res = runner.invoke(app, ["speak", str(script_file), "--voice", "justaname"])
    assert res.exit_code == 1 and "<engine>:<voice>" in res.output


def test_voices_json_edge():
    res = runner.invoke(app, ["voices", "--engine", "edge", "--lang", "en-US", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data and data[0]["ref"].startswith("edge:en-US")


def test_voices_table_unavailable_engine(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "kokoro_onnx", None)
    res = runner.invoke(app, ["voices", "--engine", "kokoro"])
    assert res.exit_code == 1 and "snipmint[offline]" in res.output


@pytest.mark.skipif(not ff.available(), reason="ffmpeg not installed")
def test_doctor_json():
    res = runner.invoke(app, ["doctor", "--json"])
    assert res.exit_code == 0, res.output
    names = {c["name"] for c in json.loads(res.output)}
    assert {"Python", "ffmpeg", "ffprobe"} <= names


def test_clean_runs():
    res = runner.invoke(app, ["clean"])
    assert res.exit_code == 0 and "Removed 0 old job folders" in res.output


@pytest.mark.skipif(not ff.available(), reason="ffmpeg not installed")
def test_speak_end_to_end_with_fake_engine(script_file, tmp_path):
    """Full CLI path using a fake engine registered under the id 'fake'."""
    from snipmint.engines import registry

    registry.register_engine("fake", "tests.unit.fake_engine:FakeEngine")
    out = tmp_path / "out"
    res = runner.invoke(
        app, ["speak", str(script_file), "--voice", "fake:v1", "--target", "7s", "-o", str(out), "--json"]
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["target_met"] is True
    for name in ("voice.mp3", "voice.wav", "words.json", "captions.srt"):
        assert (out / name).is_file()
    registry.reset_instances()
    registry.ENGINES.pop("fake", None)
