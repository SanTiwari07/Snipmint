"""Phase 2 + 5 + 6: resumable upload, retries, cancel, duplicates, batches, scheduling, restarts."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from snipmint.jobs.runner import JobRunner
from snipmint.server import create_app
from snipmint.server.youtube_api import YouTubeIntegration
from snipmint.youtube.client import YouTubeClient
from snipmint.youtube.publisher import ClipToPublish, PublishRequest, clean_tags, compose_details

from .conftest import connect, make_clip, no_wait, wait_items


def body(job_id: str, **kw) -> dict:
    clip = {"job_id": job_id, "clip_file": "clip_01.mp4", "title": "Why nobody tells you this",
            "description": "A short clip.", "hashtags": ["#Shorts", "money"], "tags": ["money", "advice"],
            "category_id": "27", "language": "en"}  # fmt: skip
    clip.update(kw.pop("clip", {}))
    return {"clips": [clip], "privacy": "unlisted", "confirmed": True, **kw}


def test_resumable_upload_in_chunks(api, google):
    connect(api, google)
    job = make_clip(api.runner, size=700_000)
    r = api.post("/api/youtube/publish", json=body(job))
    assert r.status_code == 200, r.text
    item = wait_items(api, [r.json()["items"][0]["id"]])[0]
    assert item["status"] == "PUBLISHED" and item["progress"] == 1.0
    vid = item["video_id"]
    assert item["video_url"] == f"https://www.youtube.com/shorts/{vid}"
    sent = google.videos[vid]
    folder = api.runner.workspace_for(job)
    assert sent["bytes"] == (folder / "clip_01.mp4").read_bytes()
    assert sent["snippet"]["title"] == "Why nobody tells you this"
    assert sent["snippet"]["description"].endswith("#Shorts #money")
    assert sent["snippet"]["categoryId"] == "27"
    assert sent["status"]["privacyStatus"] == "unlisted" and sent["status"]["selfDeclaredMadeForKids"] is False
    puts = [p for m, p in google.requests if m == "PUT"]
    assert len(puts) >= 3, "700 KB in 256 KiB chunks"
    assert "upload_url" not in item


def test_progress_streams_over_existing_sse(api, google):
    connect(api, google)
    r = api.post("/api/youtube/publish", json=body(make_clip(api.runner)))
    task = r.json()["task_id"]
    wait_items(api, [r.json()["items"][0]["id"]])
    with api.stream("GET", f"/api/tasks/{task}/events") as s:
        text = "".join(s.iter_text())
    assert "UPLOADING" in text and "PUBLISHED" in text and "event: end" in text


def test_requires_confirmation_and_connection(api, google):
    job = make_clip(api.runner)
    assert api.post("/api/youtube/publish", json=body(job)).status_code == 409  # not connected
    connect(api, google)
    r = api.post("/api/youtube/publish", json=body(job, confirmed=False))
    assert r.status_code >= 400 and "Confirm" in r.json()["error"]
    assert not google.sessions


def test_retries_transient_errors_with_backoff(api, google):
    connect(api, google)
    google.session_failures = [503]
    google.chunk_failures = [500, 503]
    waits: list[float] = []
    client = api.integration.manager.client
    client.wait = lambda s, c: waits.append(s) or False
    client.jitter = lambda: 1.0
    r = api.post("/api/youtube/publish", json=body(make_clip(api.runner)))
    item = wait_items(api, [r.json()["items"][0]["id"]])[0]
    assert item["status"] == "PUBLISHED"
    assert waits[:3] == [1.0, 2.0, 4.0], "exponential backoff"


def test_gives_up_after_max_retries(api, google):
    connect(api, google)
    google.chunk_failures = [503] * 20
    r = api.post("/api/youtube/publish", json=body(make_clip(api.runner)))
    item = wait_items(api, [r.json()["items"][0]["id"]])[0]
    assert item["status"] == "FAILED" and "Gave up" in item["error"]
    # retry resumes the same YouTube session and finishes
    google.chunk_failures = []
    again = api.post(f"/api/youtube/publish/{item['id']}/retry").json()
    done = wait_items(api, [again["items"][0]["id"]])[0]
    assert done["status"] == "PUBLISHED" and done["attempts"] == 2
    assert len(google.sessions) == 1, "the interrupted session was resumed, not restarted"


def test_refreshes_expired_access_token_mid_upload(api, google):
    connect(api, google)
    google.expire_once = True
    r = api.post("/api/youtube/publish", json=body(make_clip(api.runner)))
    assert wait_items(api, [r.json()["items"][0]["id"]])[0]["status"] == "PUBLISHED"


def test_quota_error_is_reported_not_retried(api, google):
    connect(api, google)
    google.quota_exceeded = True
    r = api.post("/api/youtube/publish", json=body(make_clip(api.runner)))
    item = wait_items(api, [r.json()["items"][0]["id"]])[0]
    assert item["status"] == "FAILED" and "quota" in item["error"].lower()


def test_public_upload_is_public(api, google):
    """Since the 2 Nov 2026 API revision, unverified projects are not forced to private."""
    connect(api, google)
    r = api.post("/api/youtube/publish", json=body(make_clip(api.runner), privacy="public"))
    item = wait_items(api, [r.json()["items"][0]["id"]])[0]
    sent = google.videos[item["video_id"]]
    assert item["status"] == "PUBLISHED" and item["note"] is None
    assert sent["status"]["privacyStatus"] == "public" and "publishAt" not in sent["status"]
    assert item["details"]["privacy"] == "public"


def test_visibility_youtube_changed_is_reported_honestly(api, google):
    connect(api, google)
    google.force_private = True  # e.g. a channel default or a policy review
    r = api.post("/api/youtube/publish", json=body(make_clip(api.runner), privacy="public"))
    item = wait_items(api, [r.json()["items"][0]["id"]])[0]
    assert item["status"] == "PUBLISHED"
    assert "asked for public" in item["note"] and "set this video to private" in item["note"]
    assert "verified" not in item["note"]


def test_duplicate_protection(api, google):
    connect(api, google)
    job = make_clip(api.runner)
    first = api.post("/api/youtube/publish", json=body(job)).json()
    wait_items(api, [first["items"][0]["id"]])
    dup = api.post("/api/youtube/publish", json=body(job))
    assert dup.status_code == 409 and "already" in dup.json()["error"]
    forced = api.post("/api/youtube/publish", json=body(job, allow_duplicate=True))
    assert forced.status_code == 200
    wait_items(api, [forced.json()["items"][0]["id"]])
    # the same file twice in one batch
    job2 = make_clip(api.runner, seed=7)
    two = body(job2)
    two["clips"].append({**two["clips"][0], "title": "Other"})
    assert api.post("/api/youtube/publish", json=two).status_code == 409


def test_cancel_during_upload(api, google):
    connect(api, google)
    gate, release = threading.Event(), threading.Event()
    real = google.handle

    def slow(request: httpx.Request):
        if request.method == "PUT":
            gate.set()
            release.wait(5)
        return real(request)

    api.integration.manager.client.http = httpx.Client(transport=httpx.MockTransport(slow))
    api.integration.oauth.client.http = api.integration.manager.client.http
    r = api.post("/api/youtube/publish", json=body(make_clip(api.runner, size=900_000)))
    item_id = r.json()["items"][0]["id"]
    assert gate.wait(5)
    api.post(f"/api/youtube/publish/{item_id}/cancel")
    release.set()
    item = wait_items(api, [item_id])[0]
    assert item["status"] == "CANCELLED" and not google.videos
    # published videos can't be cancelled from here
    ok = api.post("/api/youtube/publish", json=body(make_clip(api.runner, seed=3)))
    done = wait_items(api, [ok.json()["items"][0]["id"]])[0]
    res = api.post(f"/api/youtube/publish/{done['id']}/cancel")
    assert res.status_code >= 400


def test_batch_respects_concurrency_limit(tmp_path, google):
    from .mock_google import make_settings

    settings = make_settings(chunk_bytes=256 * 1024, max_retries=2, concurrency=2)
    active, peak = [0], [0]
    lock = threading.Lock()
    real = google.handle

    def counting(request: httpx.Request):
        if request.method == "PUT" and "bytes */" not in request.headers.get("content-range", ""):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            try:
                return real(request)
            finally:
                with lock:
                    active[0] -= 1
        return real(request)

    client = YouTubeClient(settings, httpx.Client(transport=httpx.MockTransport(counting)), wait=no_wait)
    runner = JobRunner(tmp_path / "jobs")
    yt = YouTubeIntegration(runner, settings=settings, client=client)
    with TestClient(create_app(runner=runner, static_dir=tmp_path / "x", youtube=yt)) as c:
        connect(c, google)
        clips = []
        for i in range(5):
            job = make_clip(runner, size=300_000, seed=i + 10)
            clips.append(body(job, clip={"title": f"Clip {i}"})["clips"][0])
        r = c.post("/api/youtube/publish", json={"clips": clips, "privacy": "private", "confirmed": True})
        items = wait_items(c, [i["id"] for i in r.json()["items"]])
    assert all(i["status"] == "PUBLISHED" for i in items)
    assert peak[0] == 2


def test_scheduled_publish_is_private_with_publish_at(api, google):
    connect(api, google)
    when = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
    r = api.post("/api/youtube/publish", json=body(make_clip(api.runner), privacy="public",
                                                   publish_at=when.isoformat()))  # fmt: skip
    assert r.status_code == 200, r.text
    item = wait_items(api, [r.json()["items"][0]["id"]])[0]
    sent = google.videos[item["video_id"]]["status"]
    assert sent["privacyStatus"] == "private"
    assert sent["publishAt"] == when.strftime("%Y-%m-%dT%H:%M:%SZ")
    listed = api.get("/api/youtube/scheduled").json()
    assert [i["id"] for i in listed] == [item["id"]] and listed[0]["scheduled"]
    # too soon / no time zone are rejected
    soon = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    assert api.post("/api/youtube/publish", json=body(make_clip(api.runner, seed=5), publish_at=soon)).status_code == 422
    naive = (datetime.now() + timedelta(days=1)).replace(tzinfo=None).isoformat()
    assert api.post("/api/youtube/publish", json=body(make_clip(api.runner, seed=6), publish_at=naive)).status_code == 422


def test_queued_schedule_survives_restart_and_can_be_cancelled(tmp_path, google, settings, yt_client):
    when = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    runner = JobRunner(tmp_path / "jobs")
    yt = YouTubeIntegration(runner, settings=settings, client=yt_client)
    gate = threading.Event()
    real = google.handle

    def blocked(request: httpx.Request):  # uploads hang: simulates the app going down mid-way
        if request.url.path.startswith("/upload"):
            gate.wait(10)
        return real(request)

    yt_client.http = httpx.Client(transport=httpx.MockTransport(blocked))
    with TestClient(create_app(runner=runner, static_dir=tmp_path / "x", youtube=yt)) as c:
        connect(c, google)
        jobs = [make_clip(runner, seed=i + 40) for i in range(2)]
        reqs = [body(j, publish_at=when, clip={"title": f"Later {i}"}) for i, j in enumerate(jobs)]
        ids = [c.post("/api/youtube/publish", json=r).json()["items"][0]["id"] for r in reqs]
        c.post(f"/api/youtube/publish/{ids[1]}/cancel")
        yt.manager.shutdown()  # the app stops
    gate.set()
    end = time.time() + 10
    while time.time() < end and yt.store.get_item(ids[1]).status != "CANCELLED":
        time.sleep(0.05)
    time.sleep(0.2)
    states = {i.id: i.status for i in yt.store.list_items()}
    assert states[ids[1]] == "CANCELLED" and states[ids[0]] in ("QUEUED", "PREPARING", "UPLOADING")

    # restart: a fresh app over the same store resumes the pending one
    runner2 = JobRunner(tmp_path / "jobs")
    client2 = YouTubeClient(settings, httpx.Client(transport=google.transport()), wait=no_wait)
    yt2 = YouTubeIntegration(runner2, settings=settings, client=client2)
    with TestClient(create_app(runner=runner2, static_dir=tmp_path / "x", youtube=yt2)) as c2:
        items = wait_items(c2, ids)
        listed = c2.get("/api/youtube/scheduled").json()
    assert items[0]["status"] == "PUBLISHED" and items[1]["status"] == "CANCELLED"
    assert "restart" in (items[0]["note"] or "") or items[0]["note"].startswith("Private until")
    assert [i["id"] for i in listed] == [ids[0]]


def test_compose_details_and_tag_limits():
    clip = ClipToPublish(job_id="j", clip_file="clip_01.mp4", title="A <b> title", description="Desc >",
                         hashtags=["#One", "two words", "#One"], tags=["x" * 90] * 3 + [f"tag{i}" for i in range(80)][:50])  # fmt: skip
    req = PublishRequest(clips=[clip], confirmed=True)
    d = compose_details(clip, req)
    assert "<" not in d.title and ">" not in d.description
    assert d.description.endswith("#One #twowords")
    assert sum(len(t) for t in d.tags) + len(d.tags) - 1 <= 500
    assert len(clean_tags(["a, b", '"q"', "#hash"])) == 3


def test_rejects_paths_outside_job_folders(api, google):
    connect(api, google)
    bad = body("..", clip={"clip_file": "clip_01.mp4"})
    assert api.post("/api/youtube/publish", json=bad).status_code >= 400
    traversal = body(make_clip(api.runner), clip={"clip_file": "../secret.key"})
    assert api.post("/api/youtube/publish", json=traversal).status_code == 422


@pytest.mark.parametrize("privacy", ["private", "unlisted", "public"])
def test_privacy_values(api, google, privacy):
    connect(api, google)
    r = api.post("/api/youtube/publish", json=body(make_clip(api.runner, seed=hash(privacy) % 200), privacy=privacy))
    item = wait_items(api, [r.json()["items"][0]["id"]])[0]
    assert google.videos[item["video_id"]]["status"]["privacyStatus"] == privacy
    assert item["note"] is None
