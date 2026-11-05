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
