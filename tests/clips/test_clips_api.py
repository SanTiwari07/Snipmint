"""End-to-end through the HTTP API on a synthetic 40 s source (no network, no Whisper)."""

import json
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from snipmint.analyze.signals import compute, extract_audio
from snipmint.audio import ffmpeg as ff
from snipmint.ingest.source import Source, SourceMeta
from snipmint.jobs.runner import JobRunner
from snipmint.server import create_app

from .helpers import make_transcript

pytestmark = [
    pytest.mark.ffmpeg,
    pytest.mark.skipif(not (ff.available() and ff.has_libass()), reason="ffmpeg with libass needed"),
]

SOURCE_ID = "yt-AAAAAAAAAAA"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPMINT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    # keep the API test offline: no face-model download, so framing falls back to a centre crop
    from snipmint.reframe import faces

    monkeypatch.setattr(faces, "available", lambda: False)
    runner = JobRunner(tmp_path / "jobs")
    with TestClient(create_app(runner=runner, static_dir=tmp_path / "no-ui")) as c:
        yield c


@pytest.fixture
def source(client):
    src = Source(SOURCE_ID)
    ff.run_ffmpeg([
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=40",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=40",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(src.video),
    ])  # fmt: skip
    sentences = [f"This is sentence number {i} about money and time." for i in range(14)]
    sentences[6] = "Why do you never see the biggest mistake people make?"
    transcript = make_transcript(sentences, word_dur=0.3, gap=0.3)
    meta = SourceMeta(id=SOURCE_ID, url="https://www.youtube.com/watch?v=AAAAAAAAAAA",
                      video_id="AAAAAAAAAAA", title="Synthetic", duration=40.0,
                      width=1280, height=720)  # fmt: skip
    src.save_meta(meta)
    src.write_json("transcript.json", transcript.model_dump())
    extract_audio(src.video, src.audio)
    src.write_json("signals.json", compute(src.video, src.audio, meta).model_dump())
    return src


def wait_for(client, job_id, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        data = client.get(f"/api/jobs/{job_id}").json()
        if data["status"] in ("done", "failed", "cancelled"):
            return data
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_bad_link_is_a_clear_400(client):
    r = client.post("/api/sources", json={"url": "https://vimeo.com/1"})
    assert r.status_code == 400
    assert "YouTube" in json.dumps(r.json())


def test_health_reports_ai_modes(client):
    data = client.get("/api/health").json()
    assert "ai" in data and "gemini_server_key" in data["ai"]


def test_analyze_preview_and_render(client, source):
    r = client.post(f"/api/sources/{SOURCE_ID}/analyze",
                    json={"provider": "heuristic", "min_seconds": 8, "max_seconds": 14, "count": 2})  # fmt: skip
    assert r.status_code == 200, r.text
    assert wait_for(client, r.json()["task_id"])["status"] == "done"

    state = client.get(f"/api/sources/{SOURCE_ID}").json()
    moments = state["analysis"]["moments"]
    assert len(moments) == 2
    assert all(8 <= m["end"] - m["start"] <= 15 for m in moments)
    assert state["status"]["stages"]["analyze"] == "done"

    m = moments[0]
    preview = client.post("/api/clips/preview", json={
        "source_id": SOURCE_ID, "start": m["start"], "end": m["end"],
        "captions": {"preset": "karaoke"}, "format": "reels",
    })  # fmt: skip
    assert preview.status_code == 200, preview.text
    assert preview.headers["content-type"] == "image/jpeg" and len(preview.content) > 1000

    clips = [{"id": x["id"], "start": x["start"], "end": x["end"], "title": x["title"],
              "keywords": x["keywords"]} for x in moments]  # fmt: skip
    r = client.post("/api/clips", json={"source_id": SOURCE_ID, "clips": clips,
                                        "captions": {"preset": "bold-pop"},
                                        "export": {"quality": "fast"}})  # fmt: skip
    assert r.status_code == 200, r.text
    job = wait_for(client, r.json()["job_id"])
    assert job["status"] == "done", job
    outs = job["result"]["clips"]
    assert [o["video"] for o in outs] == ["clip_01.mp4", "clip_02.mp4"]
    assert all(o["error"] is None for o in outs)

    video = client.get(f"/api/clips/{job['id']}/files/clip_01.mp4")
    assert video.status_code == 200 and len(video.content) > 10_000
    probe = subprocess.run(
        [ff.find_ffprobe(), "-v", "error", "-show_entries", "stream=codec_type,width,height",
         "-of", "json", "-"], input=video.content, capture_output=True, check=True,
    )  # fmt: skip
    streams = json.loads(probe.stdout)["streams"]
    v = next(s for s in streams if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1080, 1920)
    assert any(s["codec_type"] == "audio" for s in streams)

    titles = client.get(f"/api/clips/{job['id']}/files/titles.json").json()
    assert len(titles["clips"]) == 2
    assert client.get(f"/api/clips/{job['id']}/files/clips.zip").status_code == 200
    assert client.get(f"/api/clips/{job['id']}/files/../secret").status_code == 404


def _make_video(path, audio=True, seconds=20):
    args = ["-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=25:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    args += ["-c:a", "aac", "-shortest"] if audio else ["-an"]
    ff.run_ffmpeg([*args, str(path)])
    return path


def test_upload_runs_the_pipeline_without_download(client, tmp_path, monkeypatch):
    from snipmint.transcribe import whisper

    sentences = [f"This is sentence number {i} about money and time." for i in range(10)]
    monkeypatch.setattr(whisper, "transcribe", lambda *a, **k: make_transcript(sentences, word_dur=0.3, gap=0.3))
    video = _make_video(tmp_path / "talk.mkv")
    with video.open("rb") as fh:
        r = client.post("/api/sources/upload", files={"file": ("My Talk.mkv", fh, "video/x-matroska")},
                        data={"analysis": json.dumps({"provider": "heuristic", "min_seconds": 5,
                                                      "max_seconds": 10, "count": 2})})  # fmt: skip
    assert r.status_code == 200, r.text
    sid = r.json()["source_id"]
    assert sid.startswith("up-")
    assert wait_for(client, r.json()["task_id"])["status"] == "done"
    state = client.get(f"/api/sources/{sid}").json()
    assert state["meta"]["title"] == "My Talk" and state["meta"]["url"] == "file:My Talk.mkv"
    assert len(state["analysis"]["moments"]) == 2
    assert client.get(f"/api/sources/{sid}/video").headers["content-type"] == "video/mp4"
    # the same file again maps to the same source; prepare can be re-run
    r2 = client.post(f"/api/sources/{sid}/prepare", json={"provider": "heuristic"})
    assert r2.status_code == 200 and r2.json()["source_id"] == sid
    assert wait_for(client, r2.json()["task_id"])["status"] == "done"


def test_upload_rejects_silent_and_non_video_files(client, tmp_path):
    silent = _make_video(tmp_path / "silent.mp4", audio=False, seconds=3)
    with silent.open("rb") as fh:
        r = client.post("/api/sources/upload", files={"file": ("silent.mp4", fh, "video/mp4")})
    assert r.status_code == 400 and "sound" in json.dumps(r.json())
    r = client.post("/api/sources/upload", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 400 and "video file" in json.dumps(r.json())
    assert not list((tmp_path / "home" / "sources").glob(".upload-*"))


def test_gemini_key_comes_only_from_env(client, monkeypatch):
    r = client.post("/api/ai/check-key", headers={"X-Gemini-Key": "AIzaSyFROMTHEBROWSER0000000000000000"})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is False
    assert ".env" in body["error"] and "GEMINI_API_KEY" in body["error"]
    from snipmint.server import clips_api

    monkeypatch.setenv("GEMINI_API_KEY", "server-key")
    assert clips_api.gemini_key_from(None) == "server-key"  # type: ignore[arg-type]


@pytest.mark.parametrize("job_id", ["..", ".", "a/b", r"a\b", r"..\jobs", "C:", ""])
def test_job_ids_cannot_leave_the_jobs_folder(tmp_path, job_id):
    jobs = tmp_path / "jobs"
    (jobs / "2026-01-01_000000_abc123").mkdir(parents=True)
    runner = JobRunner(jobs)
    assert runner.workspace_for(job_id) is None
    assert runner.workspace_for("2026-01-01_000000_abc123") == jobs / "2026-01-01_000000_abc123"
