"""n8n automation: the 12 scenarios from the brief, with Google and n8n mocked.

n8n is simulated by calling the /api/integrations/n8n/* endpoints exactly as the workflow files
in workflows/n8n/ do, and by a mock webhook receiver for Snipmint's outgoing events.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from snipmint.automation.events import MAX_ATTEMPTS, sign
from snipmint.automation.service import READY_GRACE

from .conftest import SECRET, SID, build, h, make_job, ref, setup_ready, wait_for, wait_task

N8N = "/api/integrations/n8n"


def create(c, clips, **kw):
    r = c.post("/api/automation/tasks", json={"clips": clips, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def later(minutes: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def shift_clock(c, seconds: float) -> None:
    c.auto.service.clock = lambda: time.time() + seconds


# 1 -----------------------------------------------------------------------------------------
def test_successful_automated_publishing(env):
    c = env
    job = setup_ready(c, approve=["m01"])
    task = create(c, [ref(job, 1, "m01")])["tasks"][0]
    c.auto.dispatcher.flush()
    ready = c.n8n.of("clip.ready")
    assert ready and ready[0]["data"]["task_id"] == task["id"] and ready[0]["path"] == "/webhook/snipmint/clip-ready"

    # what workflow A does: fetch, check approval, publish, track, report
    t = c.get(f"{N8N}/tasks/{task['id']}", headers=h()).json()
    assert t["approved"] is True and t["metadata"]["title"] and t["metadata"]["hashtags"]
    r = c.post(f"{N8N}/tasks/{task['id']}/publish", headers=h(), json={"execution_id": "exec-1"}).json()
    assert r["outcome"] == "started"
    done = wait_task(c, task["id"])
    assert done["status"] == "PUBLISHED" and done["video_url"].startswith("https://www.youtube.com/shorts/")
    assert c.post(f"{N8N}/executions", headers=h(), json={"execution_id": "exec-1", "workflow": "automated-publishing",
                                                         "status": "success", "task_id": task["id"]}).status_code == 200  # fmt: skip
    assert c.get("/api/automation/executions").json()[0]["status"] == "success"
    wait_for(lambda: c.auto.dispatcher.flush() >= 0 and c.n8n.of("upload.succeeded"))
    # the upload used the existing YouTube path with the approved metadata
    video = c.google.videos[done["video_url"].rsplit("/", 1)[1]]
    assert video["snippet"]["title"] == t["metadata"]["title"]


# 2 -----------------------------------------------------------------------------------------
def test_publishing_blocked_without_approval(env):
    c = env
    job = setup_ready(c, approve=[])
    task = create(c, [ref(job, 1, "m01")])["tasks"][0]
    assert task["approved"] is False
    r = c.post(f"{N8N}/tasks/{task['id']}/publish", headers=h(), json={}).json()
    assert r["outcome"] == "awaiting_approval" and r["status"] == "AWAITING_APPROVAL" and r["label"] == "Waiting for approval"
    assert not c.google.sessions, "nothing was uploaded"
    c.auto.dispatcher.flush()
    assert c.n8n.of("metadata.awaiting_approval")
    # approving, then editing, withdraws the approval again
    c.post(f"/api/metadata/{SID}/m01/approve", json={"approved": True})
    c.put(f"/api/metadata/{SID}/m01", json={"changes": {"description": "Changed after approval."}})
    assert c.post(f"{N8N}/tasks/{task['id']}/publish", headers=h(), json={}).json()["outcome"] == "awaiting_approval"
    # once approved, the scheduler poll picks it up
    c.post(f"/api/metadata/{SID}/m01/approve", json={"approved": True})
    assert [t["id"] for t in c.get(f"{N8N}/tasks/due", headers=h()).json()] == [task["id"]]


def test_auto_approve_is_opt_in(env):
    c = env
    job = setup_ready(c, approve=[])
    c.put("/api/automation/settings", json={"auto_approve": True})
    task = create(c, [ref(job, 1, "m01")])["tasks"][0]
    r = c.post(f"{N8N}/tasks/{task['id']}/publish", headers=h(), json={}).json()
    assert r["outcome"] == "started" and r["approved_by"] == "auto"


# 3 -----------------------------------------------------------------------------------------
def test_scheduled_publishing_with_timezone(env):
    c = env
    job = setup_ready(c, approve=["m01"])
    ist = timezone(timedelta(hours=5, minutes=30))
    when = (datetime.now(ist) + timedelta(minutes=10)).replace(microsecond=0)
    task = create(c, [ref(job, 1, "m01")], run_at=when.isoformat())["tasks"][0]
    assert task["status"] == "SCHEDULED" and task["label"] == "Scheduled"
    assert task["run_at"] == when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert c.get(f"{N8N}/tasks/due", headers=h()).json() == []
    early = c.post(f"{N8N}/tasks/{task['id']}/publish", headers=h(), json={}).json()
    assert early["outcome"] == "not_due" and not c.google.sessions
    shift_clock(c, 11 * 60)
    due = c.get(f"{N8N}/tasks/due", headers=h()).json()
    assert [t["id"] for t in due] == [task["id"]]
    assert c.post(f"{N8N}/tasks/{task['id']}/publish", headers=h(), json={}).json()["outcome"] == "started"
    assert wait_task(c, task["id"])["status"] == "PUBLISHED"
    c.auto.dispatcher.flush()
    assert c.n8n.of("schedule.triggered")
    assert c.post("/api/automation/tasks", json={"clips": [ref(job, 2, "m02")],
                                                 "run_at": "2030-01-01T10:00:00"}).status_code == 409  # no time zone


# 4 -----------------------------------------------------------------------------------------
def test_cancel_and_reschedule(env):
    c = env
    job = setup_ready(c, approve=["m01"])
    task = create(c, [ref(job, 1, "m01")], run_at=later(5))["tasks"][0]
    assert c.post(f"/api/automation/tasks/{task['id']}/cancel").json()["status"] == "CANCELLED"
    shift_clock(c, 3600)
    assert c.get(f"{N8N}/tasks/due", headers=h()).json() == []
    assert c.post(f"{N8N}/tasks/{task['id']}/publish", headers=h(), json={}).json()["outcome"] == "skipped"
    assert not c.google.sessions
    c.auto.service.clock = time.time
    r = c.post(f"/api/automation/tasks/{task['id']}/reschedule", json={"run_at": later(30)}).json()
    assert r["status"] == "SCHEDULED"


# 5 -----------------------------------------------------------------------------------------
def test_batch_with_mixed_results(env):
    c = env
    job = setup_ready(c, approve=["m01", "m03"])
    res = create(c, [ref(job, 1, "m01"), ref(job, 2, "m02"), ref(job, 3, "m03")], batch=True, delay_seconds=0,
                 concurrency=2)  # fmt: skip
    batch_id = res["batch_id"]
    c.auto.dispatcher.flush()
    assert c.n8n.of("batch.created")[0]["data"]["batch_id"] == batch_id
    # clip 3 disappears before its turn (e.g. old jobs cleaned up): it must fail alone
    (c.runner.workspace_for(job) / "clip_03.mp4").unlink()
    b = c.get(f"{N8N}/batches/{batch_id}", headers=h()).json()
    for t in b["tasks"]:  # workflow C: one by one, continuing after failures
        c.post(f"{N8N}/tasks/{t['id']}/publish", headers=h(), json={"execution_id": "batch-exec"})
    for t in b["tasks"]:
        wait_task(c, t["id"], ("PUBLISHED", "FAILED", "CANCELLED", "SKIPPED", "AWAITING_APPROVAL"))
    wait_for(lambda: c.get(f"{N8N}/batches/{batch_id}", headers=h()).json()["completed_at"])
    s = c.get(f"{N8N}/batches/{batch_id}", headers=h()).json()["summary"]
    assert (s["succeeded"], s["failed"], s["skipped"], s["pending"]) == (1, 1, 1, 0)
    wait_for(lambda: c.auto.dispatcher.flush() >= 0 and c.n8n.of("batch.completed"))
    done = c.n8n.of("batch.completed")
    assert len(done) == 1 and done[0]["data"]["succeeded"] == 1


# 6 -----------------------------------------------------------------------------------------
def test_n8n_unavailable_does_not_break_snipmint(env):
    c = env
    c.n8n.down = True
    job = setup_ready(c, approve=["m01"])
    st = c.get("/api/automation/status").json()
    assert st["configured"] and st["reachable"] is False
    task = create(c, [ref(job, 1, "m01")])["tasks"][0]  # still works
    c.auto.dispatcher.flush()
    ev = c.auto.store.list_events()[-1]
    assert ev.status == "pending" and "ConnectError" in ev.last_error and ev.next_attempt_at > time.time()
    # manual publishing is untouched by n8n being down
    manual = c.post("/api/youtube/publish", json={"clips": [{"job_id": job, "clip_file": "clip_02.mp4", "title": "Manual"}],
                                                   "privacy": "private", "confirmed": True})  # fmt: skip
    assert manual.status_code == 200
    # n8n comes back: the outbox delivers what it missed; and /due offers the task it never saw
    c.n8n.down = False
    for e in c.auto.store.list_events():
        e.next_attempt_at = 0
        c.auto.store.save_event(e)
    c.auto.dispatcher.flush()
    assert c.n8n.of("clip.ready")
    shift_clock(c, READY_GRACE + 1)
    assert task["id"] in [t["id"] for t in c.get(f"{N8N}/tasks/due", headers=h()).json()]


def test_outbox_gives_up_after_max_attempts(env):
    c = env
    c.n8n.down = True
    ev = c.auto.dispatcher.emit("upload.failed", {"task_id": "x"})
    for _ in range(MAX_ATTEMPTS):
        e = next(x for x in c.auto.store.list_events() if x.id == ev.id)
        e.next_attempt_at = 0
        c.auto.store.save_event(e)
        c.auto.dispatcher.flush()
    assert next(x for x in c.auto.store.list_events() if x.id == ev.id).status == "dead"
    assert c.post("/api/automation/events/retry").json()["requeued"] >= 1


# 7 -----------------------------------------------------------------------------------------
def test_invalid_webhook_authentication(env, tmp_path):
    c = env
    assert c.get(f"{N8N}/health").status_code == 401
    assert c.get(f"{N8N}/health", headers={"X-Snipmint-Secret": "wrong"}).status_code == 401
    assert c.get(f"{N8N}/health", headers=h()).json()["ok"] is True
    body = json.dumps({"execution_id": "e1", "workflow": "x", "status": "running"}).encode()
    ts = str(int(time.time()))
    good = {"X-Snipmint-Timestamp": ts, "X-Snipmint-Signature": sign(SECRET, ts, body), "Content-Type": "application/json"}
    assert c.post(f"{N8N}/executions", content=body, headers=good).status_code == 200
    old = str(int(time.time()) - 3600)
    stale = {**good, "X-Snipmint-Timestamp": old, "X-Snipmint-Signature": sign(SECRET, old, body)}
    assert c.post(f"{N8N}/executions", content=body, headers=stale).status_code == 401
    tampered = {**good, "X-Snipmint-Signature": sign(SECRET, ts, body + b" ")}
    assert c.post(f"{N8N}/executions", content=body, headers=tampered).status_code == 401
    # invalid payloads are rejected after authentication
    bad = c.post(f"{N8N}/executions", headers=h(), json={"execution_id": "", "status": "hacked"})
    assert bad.status_code == 422 and bad.json()["type"] == "ValidationError"


def test_off_without_secret_and_app_token_not_needed(tmp_path, google, n8n):
    app, *_ = build(tmp_path, google, n8n, secret="")
    with TestClient(app) as c:
        assert c.get(f"{N8N}/health", headers=h()).status_code == 503
        assert c.get("/api/automation/status").json()["configured"] is False
        assert c.get("/api/automation/tasks").json() == []


def test_n8n_routes_use_their_own_secret_not_the_app_token(tmp_path, google, n8n):
    from snipmint.server import create_app

    _, runner, yt, auto = build(tmp_path, google, n8n)
    app = create_app(runner=runner, static_dir=tmp_path / "x", token="app-token-123", youtube=yt, automation=auto)
    with TestClient(app) as c:
        assert c.get(f"{N8N}/health", headers=h()).status_code == 200  # no app token needed
        assert c.get("/api/automation/status").status_code == 401  # UI routes still need it
        assert c.get("/api/youtube/status", headers=h()).status_code == 401  # the secret opens nothing else


def test_rate_limit(env):
    c = env
    c.auto.limiter.capacity, c.auto.limiter.rate = 3.0, 0.0001
    codes = [c.get(f"{N8N}/health", headers=h()).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[-1] == 429


# 8 -----------------------------------------------------------------------------------------
def test_duplicate_webhook_delivery_uploads_once(env):
    c = env
    job = setup_ready(c, approve=["m01"])
    task = create(c, [ref(job, 1, "m01")])["tasks"][0]
    key = {"Idempotency-Key": f"{task['id']}:publish:1"}
    first = c.post(f"{N8N}/tasks/{task['id']}/publish", headers=h(**key), json={})
    again = c.post(f"{N8N}/tasks/{task['id']}/publish", headers=h(**key), json={})
    assert again.headers.get("idempotent-replay") == "true" and again.json() == first.json()
    # a redelivery without the key is also harmless
    third = c.post(f"{N8N}/tasks/{task['id']}/publish", headers=h(), json={}).json()
    assert third["outcome"] in ("already_started", "already_published")
    wait_task(c, task["id"])
    assert len(c.google.sessions) == 1 and len(c.google.videos) == 1
    # a second task for the same clip file is refused while the first is open / skipped as duplicate after
    dup = create(c, [ref(job, 1, "m01")])["tasks"][0]
    assert c.post(f"{N8N}/tasks/{dup['id']}/publish", headers=h(), json={}).json()["outcome"] == "duplicate"
    assert len(c.google.videos) == 1


# 9 + 10 ------------------------------------------------------------------------------------
def test_upload_timeout_then_retry_with_backoff(tmp_path, google, n8n):
    state = {"timeout": True}

    def flaky(request: httpx.Request):
        if state["timeout"] and request.method == "PUT":
            raise httpx.ReadTimeout("timed out", request=request)
        return google.handle(request)

    app, runner, yt, auto = build(tmp_path, google, n8n, google_transport=httpx.MockTransport(flaky))
    with TestClient(app) as c:
        c.runner, c.yt, c.auto, c.google, c.n8n = runner, yt, auto, google, n8n
        job = setup_ready(c, approve=["m01"])
        task = create(c, [ref(job, 1, "m01")])["tasks"][0]
        c.post(f"{N8N}/tasks/{task['id']}/publish", headers=h(), json={})
        failed = wait_task(c, task["id"], ("FAILED",))
        assert failed["attempts"] == 1 and failed["next_attempt_at"] and failed["error_kind"] == "Retryable"
        c.auto.dispatcher.flush()
        assert c.n8n.of("upload.failed")[0]["data"]["will_retry"] is True
        # not yet: backoff
        assert c.post(f"{N8N}/tasks/{task['id']}/publish", headers=h(), json={}).json()["outcome"] == "retry_later"
        assert c.get(f"{N8N}/tasks/due", headers=h()).json() == []
        state["timeout"] = False
        shift_clock(c, 61)
        assert [t["id"] for t in c.get(f"{N8N}/tasks/due", headers=h()).json()] == [task["id"]]
        assert c.post(f"{N8N}/tasks/{task['id']}/publish", headers=h(), json={}).json()["outcome"] == "started"
        done = wait_task(c, task["id"])
        assert done["status"] == "PUBLISHED" and done["attempts"] == 2
        assert len(google.sessions) == 1, "the retry resumed the same YouTube upload session"


def test_retries_stop_at_the_attempt_limit(tmp_path, google, n8n):
    def always_timeout(request: httpx.Request):
        if request.method == "PUT":
            raise httpx.ReadTimeout("timed out", request=request)
        return google.handle(request)

    app, runner, yt, auto = build(tmp_path, google, n8n, google_transport=httpx.MockTransport(always_timeout))
    with TestClient(app) as c:
        c.runner, c.yt, c.auto, c.google, c.n8n = runner, yt, auto, google, n8n
        job = setup_ready(c, approve=["m01"])
        c.put("/api/automation/settings", json={"max_attempts": 2})
        task = create(c, [ref(job, 1, "m01")])["tasks"][0]
        offset = 0
        for _ in range(2):
            c.post(f"{N8N}/tasks/{task['id']}/publish", headers=h(), json={})
            wait_task(c, task["id"], ("FAILED",))
            offset += 3700
            shift_clock(c, offset)
        t = c.get(f"{N8N}/tasks/{task['id']}", headers=h()).json()
        assert t["attempts"] == 2 and t["next_attempt_at"] is None
        assert c.get(f"{N8N}/tasks/due", headers=h()).json() == []
        assert c.post(f"{N8N}/tasks/{task['id']}/publish", headers=h(), json={}).json()["outcome"] == "no_more_retries"
        # the person can still retry by hand (fresh budget)
        assert c.post(f"/api/automation/tasks/{task['id']}/retry").json()["attempts"] == 1


# 11 ----------------------------------------------------------------------------------------
def test_pending_schedules_survive_restarts(tmp_path, google, n8n):
    app, runner, yt, auto = build(tmp_path, google, n8n)
    with TestClient(app) as c:
        c.runner, c.yt, c.auto, c.google, c.n8n = runner, yt, auto, google, n8n
        job = setup_ready(c, approve=["m01", "m02"])
        t1 = create(c, [ref(job, 1, "m01")], run_at=later(3))["tasks"][0]
        t2 = create(c, [ref(job, 2, "m02")], run_at=later(4))["tasks"][0]
    # n8n and Snipmint both restart; n8n holds no state, Snipmint reloads it from disk
    app2, runner2, yt2, auto2 = build(tmp_path, google, n8n)
    with TestClient(app2) as c2:
        c2.auto = auto2
        listed = {t["id"]: t["status"] for t in c2.get("/api/automation/tasks").json()}
        assert listed == {t1["id"]: "SCHEDULED", t2["id"]: "SCHEDULED"}
        shift_clock(c2, 10 * 60)
        due = [t["id"] for t in c2.get(f"{N8N}/tasks/due", headers=h()).json()]
        assert due == [t1["id"], t2["id"]]  # soonest first
        for tid in due:
            assert c2.post(f"{N8N}/tasks/{tid}/publish", headers=h(), json={}).json()["outcome"] == "started"
        assert {wait_task(c2, tid)["status"] for tid in due} == {"PUBLISHED"}


def test_missed_schedule_beyond_lateness_is_skipped(env):
    c = env
    job = setup_ready(c, approve=["m01"])
    task = create(c, [ref(job, 1, "m01")], run_at=later(2))["tasks"][0]
    shift_clock(c, 2 * 24 * 3600)
    r = c.post(f"{N8N}/tasks/{task['id']}/publish", headers=h(), json={}).json()
    assert r["outcome"] == "missed" and r["status"] == "SKIPPED" and not c.google.sessions


# 12 ----------------------------------------------------------------------------------------
def test_notification_delivery_is_signed(env):
    c = env
    job = setup_ready(c, approve=["m01"])
    task = create(c, [ref(job, 1, "m01")])["tasks"][0]
    c.post(f"{N8N}/tasks/{task['id']}/publish", headers=h(), json={})
    wait_task(c, task["id"])
    wait_for(lambda: c.auto.dispatcher.flush() >= 0 and c.n8n.of("upload.succeeded"))
    req = next(r for r in c.n8n.requests if r.headers.get("x-snipmint-event") == "upload.succeeded")
    assert req.url.path == "/webhook/snipmint/notify"
    ts = req.headers["x-snipmint-timestamp"]
    expected = "sha256=" + hmac.new(SECRET.encode(), f"{ts}.".encode() + req.content, hashlib.sha256).hexdigest()
    assert req.headers["x-snipmint-signature"] == expected
    data = json.loads(req.content)["data"]
    assert data["status"] == "PUBLISHED" and data["video_url"] and "token" not in json.dumps(data).lower()


def test_secrets_never_logged_or_returned(env, caplog):
    c = env
    caplog.set_level(logging.DEBUG)
    c.get(f"{N8N}/health", headers={"X-Snipmint-Secret": "wrong-secret-value-xyz"})
    c.get(f"{N8N}/health", headers=h())
    logging.getLogger("snipmint.test").info("leak attempt %s and Bearer abcdefghijkl", SECRET)
    assert SECRET not in caplog.text and "abcdefghijkl" not in caplog.text
    assert SECRET not in json.dumps(c.get("/api/automation/status").json())


def test_unknown_task_and_validation(env):
    c = env
    setup_ready(c, approve=[])
    assert c.get(f"{N8N}/tasks/nope", headers=h()).status_code == 404
    bad = c.post("/api/automation/tasks", json={"clips": [{"job_id": "x", "clip_file": "../etc/passwd", "clip_id": "m",
                                                           "source_id": SID}]})  # fmt: skip
    assert bad.status_code == 422
    missing = c.post("/api/automation/tasks", json={"clips": [ref("2020-01-01_000000_aaaaaa", 1, "m01")]})
    assert missing.status_code >= 400
    c.put("/api/automation/settings", json={"enabled": False})
    job = make_job(c.runner, [("m01", 0, 8)], seed=50)
    off = c.post("/api/automation/tasks", json={"clips": [ref(job, 1, "m01")]})
    assert off.status_code == 409 and "turned off" in off.json()["error"]
