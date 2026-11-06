from __future__ import annotations

import os
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from snipmint.jobs.runner import JobRunner
from snipmint.jobs.workspace import Workspace
from snipmint.server import create_app
from snipmint.server.youtube_api import YouTubeIntegration
from snipmint.youtube.client import YouTubeClient

from .mock_google import MockGoogle, make_settings


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPMINT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SNIPMINT_SECRET_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def google() -> MockGoogle:
    return MockGoogle()


def no_wait(_seconds, cancel):
    return bool(cancel is not None and cancel.is_set())


@pytest.fixture
def settings():
    return make_settings(chunk_bytes=256 * 1024, max_retries=4)


@pytest.fixture
def yt_client(google, settings):
    return YouTubeClient(settings, httpx.Client(transport=google.transport()), wait=no_wait, jitter=lambda: 0.0)


@pytest.fixture
def api(tmp_path, settings, yt_client):
    runner = JobRunner(tmp_path / "jobs")
    integration = YouTubeIntegration(runner, settings=settings, client=yt_client)
    with TestClient(create_app(runner=runner, static_dir=tmp_path / "no-ui", youtube=integration)) as c:
        c.runner = runner
        c.integration = integration
        yield c


def connect(client: TestClient, google: MockGoogle) -> dict:
    """Run the whole sign-in: start -> Google consent -> callback. Returns the callback redirect query."""
    r = client.post("/api/youtube/oauth/start", json={"return_to": "/publish"})
    assert r.status_code == 200, r.text
    code, state = google.consent(r.json()["auth_url"])
    cb = client.get("/api/youtube/oauth/callback", params={"state": state, "code": code}, follow_redirects=False)
    assert cb.status_code == 303
    return {k: v[0] for k, v in parse_qs(urlparse(cb.headers["location"]).query).items()}


def make_clip(runner: JobRunner, size: int = 600_000, seed: int = 1, name: str = "clip_01.mp4") -> str:
    ws = Workspace.create(runner.jobs_base)
    ws.file(name).write_bytes(os.urandom(8) + bytes([seed]) * size)
    return ws.id


def wait_items(client: TestClient, ids: list[str], timeout: float = 20.0) -> list[dict]:
    end = time.time() + timeout
    while time.time() < end:
        items = [client.get(f"/api/youtube/publish/{i}").json() for i in ids]
        if all(i["status"] in ("PUBLISHED", "FAILED", "CANCELLED") for i in items):
            return items
        time.sleep(0.05)
    raise AssertionError(f"uploads did not finish: {[i['status'] for i in items]}")
