"""End to end over real HTTP: Snipmint configured only through environment variables, talking to
a mock Google/YouTube server on 127.0.0.1. Connect, publish a batch, schedule, disconnect."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from snipmint.jobs.runner import JobRunner
from snipmint.server import create_app

from .conftest import connect, make_clip, wait_items
from .mock_google import CLIENT_ID, CLIENT_SECRET, REDIRECT, MockGoogleServer


def test_full_flow_against_mock_youtube_server(tmp_path, monkeypatch):
    with MockGoogleServer() as srv:
        base = srv.base
        for k, v in {
            "GOOGLE_CLIENT_ID": CLIENT_ID, "GOOGLE_CLIENT_SECRET": CLIENT_SECRET, "GOOGLE_REDIRECT_URI": REDIRECT,
            "YOUTUBE_AUTH_URL": f"{base}/o/oauth2/v2/auth", "YOUTUBE_TOKEN_URL": f"{base}/token",
            "YOUTUBE_REVOKE_URL": f"{base}/revoke", "YOUTUBE_API_URL": f"{base}/youtube/v3",
            "YOUTUBE_UPLOAD_URL": f"{base}/upload/youtube/v3/videos", "YOUTUBE_UPLOAD_CONCURRENCY": "2",
            "SNIPMINT_SECRET_KEY": "integration-test-passphrase",
        }.items():  # fmt: skip
            monkeypatch.setenv(k, v)
        runner = JobRunner(tmp_path / "jobs")
        with TestClient(create_app(runner=runner, static_dir=tmp_path / "no-ui")) as c:
            assert c.get("/api/youtube/status").json()["configured"] is True
            assert connect(c, srv.google)["youtube"] == "connected"

            clips = []
            for i in range(3):
                job = make_clip(runner, size=400_000, seed=i + 90)
                clips.append({"job_id": job, "clip_file": "clip_01.mp4", "title": f"Real HTTP clip {i}",
                              "hashtags": ["#Shorts"], "tags": ["test"]})  # fmt: skip
            r = c.post("/api/youtube/publish", json={"clips": clips, "privacy": "private", "confirmed": True})
            assert r.status_code == 200, r.text
            items = wait_items(c, [i["id"] for i in r.json()["items"]], timeout=60)
            assert [i["status"] for i in items] == ["PUBLISHED"] * 3
            assert {i["video_id"] for i in items} <= set(srv.google.videos)

            when = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
            job = make_clip(runner, seed=99)
            r = c.post("/api/youtube/publish", json={"clips": [{"job_id": job, "clip_file": "clip_01.mp4",
                       "title": "Later"}], "privacy": "public", "publish_at": when, "confirmed": True})  # fmt: skip
            sched = wait_items(c, [r.json()["items"][0]["id"]], timeout=60)[0]
            assert srv.google.videos[sched["video_id"]]["status"]["publishAt"].endswith("Z")
            assert len(c.get("/api/youtube/scheduled").json()) == 1

            assert c.post("/api/youtube/disconnect", json={}).json()["revoked"] is True
            assert c.get("/api/youtube/status").json()["channels"] == []
