from __future__ import annotations

import json
import os
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from snipmint.automation.events import EventDispatcher
from snipmint.automation.settings import AutomationEnv
from snipmint.automation.store import JsonAutomationStore
from snipmint.ingest.source import Source, SourceMeta
from snipmint.jobs.runner import JobRunner
from snipmint.jobs.workspace import Workspace
from snipmint.server import create_app
from snipmint.server.automation_api import AutomationIntegration
from snipmint.server.youtube_api import YouTubeIntegration
from snipmint.youtube.client import YouTubeClient
from tests.clips.helpers import make_transcript
from tests.youtube.conftest import connect, no_wait
from tests.youtube.mock_google import MockGoogle, make_settings

SECRET = "test-n8n-shared-secret-0123456789"
SID = "yt-CCCCCCCCCCC"
SENTENCES = [
    "Why do most people never save any money at all?", "Because nobody teaches budgeting in school.",
    "Start by tracking every expense for one month.", "Then move ten percent of your salary into savings first.",
    "Compound interest does the rest over twenty years.", "Investing early beats investing big, every single time.",
    "My first salary was small, but I still saved some of it.", "Debt is the real enemy of saving money.",
]  # fmt: skip


class MockN8n:
    """Receives Snipmint's events like n8n's webhook endpoints would."""

    def __init__(self):
        self.events: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.down = False
        self.lock = threading.Lock()

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        with self.lock:
            self.requests.append(request)
            if request.url.path == "/healthz":
                return httpx.Response(200, json={"status": "ok"})
            if request.headers.get("x-snipmint-secret") != SECRET:
                return httpx.Response(403, json={"message": "Authorization data is wrong!"})
            body = json.loads(request.content)
            self.events.append({"path": request.url.path, **body})
        return httpx.Response(200, json={"message": "Workflow was started"})

    def of(self, event_type: str) -> list[dict]:
        with self.lock:
            return [e for e in self.events if e["type"] == event_type]


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPMINT_HOME", str(tmp_path / "home"))
    for name in ("SNIPMINT_SECRET_KEY", "GEMINI_API_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
                 "GOOGLE_REDIRECT_URI", "SNIPMINT_N8N_WEBHOOK_SECRET", "N8N_WEBHOOK_URL"):  # fmt: skip
        monkeypatch.delenv(name, raising=False)


def build(tmp_path, google: MockGoogle, n8n: MockN8n, *, google_transport=None, secret: str = SECRET):
    settings = make_settings(chunk_bytes=256 * 1024, max_retries=2)
    yt_client = YouTubeClient(settings, httpx.Client(transport=google_transport or google.transport()), wait=no_wait,
                              jitter=lambda: 0.0)  # fmt: skip
    runner = JobRunner(tmp_path / "jobs")
    yt = YouTubeIntegration(runner, settings=settings, client=yt_client)
    env = AutomationEnv(secret=secret, webhook_url="http://n8n.test/webhook", health_url="http://n8n.test/healthz")
    store = JsonAutomationStore(tmp_path / "home" / "integrations" / "automation")
    dispatcher = EventDispatcher(env, store, http=httpx.Client(transport=httpx.MockTransport(n8n.handle)))
    auto = AutomationIntegration(yt, env=env, store=store, dispatcher=dispatcher)
    app = create_app(runner=runner, static_dir=tmp_path / "no-ui", youtube=yt, automation=auto)
    return app, runner, yt, auto


@pytest.fixture
def google():
    return MockGoogle()


@pytest.fixture
def n8n():
    return MockN8n()


@pytest.fixture
def env(tmp_path, google, n8n):
    app, runner, yt, auto = build(tmp_path, google, n8n)
    with TestClient(app) as c:
        c.runner, c.yt, c.auto, c.google, c.n8n = runner, yt, auto, google, n8n
        yield c


def make_source() -> Source:
    src = Source(SID)
    src.save_meta(SourceMeta(id=SID, url="https://www.youtube.com/watch?v=CCCCCCCCCCC", video_id="CCCCCCCCCCC",
                             title="Money Talks", channel="Money Talks", duration=40.0))  # fmt: skip
    src.write_json("transcript.json", json.loads(make_transcript(SENTENCES).model_dump_json()))
    return src


def make_job(runner: JobRunner, clips: list[tuple[str, float, float]], seed: int = 1) -> str:
    """A finished clips job with real files: [(moment id, start, end)]."""
    ws = Workspace.create(runner.jobs_base)
    out = []
    for i, (mid, start, end) in enumerate(clips, start=1):
        name = f"clip_{i:02d}.mp4"
        ws.file(name).write_bytes(os.urandom(8) + bytes([seed + i]) * 300_000)
        out.append({"id": mid, "index": i, "title": mid, "start": start, "end": end, "video": name})
    ws.write_json("job.json", {"id": ws.id, "kind": "clips", "status": "done", "result": {"clips": out}})
    return ws.id


def ref(job_id: str, n: int, clip_id: str) -> dict:
    return {"job_id": job_id, "clip_file": f"clip_{n:02d}.mp4", "clip_id": clip_id, "source_id": SID}


def h(**extra) -> dict:
    return {"X-Snipmint-Secret": SECRET, **extra}


def setup_ready(c, approve: list[str], clips=(("m01", 0, 8), ("m02", 8, 16), ("m03", 16, 24)), on: bool = True):
    """Connected channel, source with metadata for every clip, some approved, automation on."""
    connect(c, c.google)
    make_source()
    job = make_job(c.runner, list(clips))
    r = c.post(f"/api/metadata/{SID}/generate", json={"clips": [{"clip_id": m, "start": s, "end": e} for m, s, e in clips],
                                                      "language": "en"})  # fmt: skip
    assert r.status_code == 200, r.text
    for m in approve:
        assert c.post(f"/api/metadata/{SID}/{m}/approve", json={"approved": True}).status_code == 200
    if on:
        assert c.put("/api/automation/settings", json={"enabled": True}).status_code == 200
    return job


def wait_task(c, task_id: str, statuses=("PUBLISHED", "FAILED", "CANCELLED", "SKIPPED"), timeout=20.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        t = c.get(f"/api/integrations/n8n/tasks/{task_id}", headers=h()).json()
        if t["status"] in statuses:
            return t
        time.sleep(0.05)
    raise AssertionError(f"task stuck in {t['status']}")


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met")
