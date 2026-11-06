"""Phase 1: connect / disconnect a channel, token storage and refresh."""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from snipmint.jobs.runner import JobRunner
from snipmint.server import create_app
from snipmint.youtube.secrets import SecretBox, SecretError
from snipmint.youtube.settings import SCOPES

from .conftest import connect


def test_not_configured_is_harmless(tmp_path):
    with TestClient(create_app(runner=JobRunner(tmp_path / "jobs"), static_dir=tmp_path / "x")) as c:
        st = c.get("/api/youtube/status").json()
        assert st["configured"] is False and st["channels"] == []
        r = c.post("/api/youtube/oauth/start", json={})
        assert r.status_code == 409 and "GOOGLE_CLIENT_ID" in r.json()["error"]
        assert c.get("/api/youtube/publish").json() == []
        assert c.get("/api/health").status_code == 200
    # nothing about integrations is written when the feature is off
    assert not (tmp_path / "home" / "integrations").exists()


def test_start_builds_pkce_url_and_sets_cookie(api):
    r = api.post("/api/youtube/oauth/start", json={"return_to": "/publish"})
    q = {k: v[0] for k, v in parse_qs(urlparse(r.json()["auth_url"]).query).items()}
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert set(q["scope"].split()) == set(SCOPES)
    assert q["access_type"] == "offline" and len(q["state"]) >= 32
    assert "snipmint_yt_oauth" in r.cookies
    assert "code_verifier" not in r.text


def test_connect_stores_encrypted_tokens_and_hides_them(api, google, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    q = connect(api, google)
    assert q["youtube"] == "connected" and q["channel"] == "Test Channel"
    st = api.get("/api/youtube/status").json()
    assert st["channels"][0]["id"] == "UC_test_channel" and st["default_channel"] == "UC_test_channel"
    tokens = list(google.access) + list(google.refresh)
    blob = (tmp_path / "home" / "integrations" / "youtube" / "accounts.json").read_text()
    for t in tokens:
        assert t not in blob, "tokens must be encrypted at rest"
        assert t not in str(st)
        assert t not in caplog.text, "tokens must never be logged"
    assert "token_sealed" not in str(st)


def test_callback_rejects_bad_state_and_foreign_browser(api, google, settings):
    r = api.post("/api/youtube/oauth/start", json={})
    code, state = google.consent(r.json()["auth_url"])
    bad = api.get("/api/youtube/oauth/callback", params={"state": "forged", "code": code}, follow_redirects=False)
    assert "youtube=error" in bad.headers["location"]
    # a different browser (no binding cookie) cannot finish someone else's sign-in
    other = TestClient(api.app)
    res = other.get("/api/youtube/oauth/callback", params={"state": state, "code": code}, follow_redirects=False)
    assert "youtube=error" in res.headers["location"] and "different+browser" in res.headers["location"]
    # the state was consumed: replaying it in the right browser fails too
    again = api.get("/api/youtube/oauth/callback", params={"state": state, "code": code}, follow_redirects=False)
    assert "expired" in again.headers["location"]
    assert api.get("/api/youtube/status").json()["channels"] == []


def test_user_cancel_on_consent_screen(api):
    r = api.post("/api/youtube/oauth/start", json={})
    state = parse_qs(urlparse(r.json()["auth_url"]).query)["state"][0]
    res = api.get("/api/youtube/oauth/callback", params={"state": state, "error": "access_denied"},
                  follow_redirects=False)  # fmt: skip
    assert "cancelled" in res.headers["location"]


def test_open_redirect_is_blocked(api, google):
    r = api.post("/api/youtube/oauth/start", json={"return_to": "//evil.example/x"})
    code, state = google.consent(r.json()["auth_url"])
    res = api.get("/api/youtube/oauth/callback", params={"state": state, "code": code}, follow_redirects=False)
    assert res.headers["location"].startswith("/publish?")


def test_refresh_and_disconnect_revokes(api, google):
    connect(api, google)
    oauth = api.integration.oauth
    first = oauth.access_token()
    refreshed = oauth.access_token(force=True)
    assert refreshed != first and refreshed in google.access
    r = api.post("/api/youtube/disconnect", json={}).json()
    assert r == {"disconnected": True, "revoked": True}
    assert google.revoked and google.revoked[0].startswith("1//refresh-")
    assert api.get("/api/youtube/status").json()["channels"] == []


def test_token_requires_bearer_except_callback(tmp_path, settings, yt_client):
    from snipmint.server.youtube_api import YouTubeIntegration

    runner = JobRunner(tmp_path / "jobs")
    yt = YouTubeIntegration(runner, settings=settings, client=yt_client)
    with TestClient(create_app(runner=runner, static_dir=tmp_path / "x", token="s3cret", youtube=yt)) as c:
        assert c.get("/api/youtube/status").status_code == 401
        # the callback is reachable without the API token (Google redirects the browser), but still checks state
        res = c.get("/api/youtube/oauth/callback", params={"state": "x", "code": "y"}, follow_redirects=False)
        assert res.status_code == 303 and "youtube=error" in res.headers["location"]


def test_secret_box_roundtrip_and_wrong_key(tmp_path, monkeypatch):
    box = SecretBox(folder=tmp_path)
    sealed = box.seal({"access_token": "abc"})
    assert "abc" not in sealed and box.open(sealed) == {"access_token": "abc"}
    monkeypatch.setenv("SNIPMINT_SECRET_KEY", "a different passphrase")
    with pytest.raises(SecretError):
        SecretBox().open(sealed)
