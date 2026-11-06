"""Development vs production configuration and redirect-URI rules."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from snipmint import config
from snipmint.jobs.runner import JobRunner
from snipmint.server import create_app
from snipmint.youtube import settings as yt


@pytest.mark.parametrize(
    ("uri", "ok"),
    [
        ("http://localhost:8000/api/youtube/oauth/callback", True),
        ("http://127.0.0.1:5173/api/youtube/oauth/callback", True),
        ("http://[::1]:8000/api/youtube/oauth/callback", True),
        ("https://clips.example.com/api/youtube/oauth/callback", True),
        ("http://clips.example.com/api/youtube/oauth/callback", False),  # Google requires https off loopback
        ("http://192.168.1.10:8000/api/youtube/oauth/callback", False),
        ("https://clips.example.com/somewhere/else", False),
        ("https://clips.example.com/api/youtube/oauth/callback?x=1", False),
        ("ftp://localhost/api/youtube/oauth/callback", False),
    ],
)
def test_redirect_rules(uri, ok):
    assert (yt.redirect_problem(uri) is None) is ok


def _google_env(monkeypatch, redirect: str | None):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    if redirect is None:
        monkeypatch.delenv("GOOGLE_REDIRECT_URI", raising=False)
    else:
        monkeypatch.setenv("GOOGLE_REDIRECT_URI", redirect)


def test_dev_defaults_to_a_localhost_callback(monkeypatch):
    _google_env(monkeypatch, None)
    monkeypatch.setenv("SNIPMINT_PORT", "8123")
    dev = yt.load(dev=True)
    assert dev.redirect_uri == "http://localhost:8123/api/youtube/oauth/callback"
    assert dev.configured and dev.environment == "development"
    prod = yt.load(dev=False)
    assert prod.redirect_uri == "" and not prod.configured and prod.environment == "production"


def test_production_rejects_plain_http_on_a_public_host(tmp_path, monkeypatch):
    _google_env(monkeypatch, "http://clips.example.com/api/youtube/oauth/callback")
    with TestClient(create_app(runner=JobRunner(tmp_path / "jobs"), static_dir=tmp_path / "x")) as c:
        st = c.get("/api/youtube/status").json()
        assert st["configured"] is False and "https" in st["config_error"]
        r = c.post("/api/youtube/oauth/start", json={})
        assert r.status_code == 409 and "https" in r.json()["error"]


def test_dev_app_reports_development(tmp_path, monkeypatch):
    _google_env(monkeypatch, None)
    with TestClient(create_app(runner=JobRunner(tmp_path / "jobs"), static_dir=tmp_path / "x", dev=True)) as c:
        st = c.get("/api/youtube/status").json()
    assert st["configured"] and st["environment"] == "development"
    assert st["redirect_uri"].startswith("http://localhost:")


def test_env_file_loader(tmp_path, monkeypatch):
    f = tmp_path / ".env.development"
    f.write_text(
        "# comment\n\nexport SNIPMINT_TEST_A=one\nSNIPMINT_TEST_B=\"two words\"\n"
        "SNIPMINT_TEST_C=three # trailing\nSNIPMINT_TEST_D=from-file\nnot a line\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SNIPMINT_TEST_D", "from-env")
    for k in ("SNIPMINT_TEST_A", "SNIPMINT_TEST_B", "SNIPMINT_TEST_C"):
        monkeypatch.delenv(k, raising=False)
    names = config.load_env_file(f)
    assert os.environ["SNIPMINT_TEST_A"] == "one"
    assert os.environ["SNIPMINT_TEST_B"] == "two words"
    assert os.environ["SNIPMINT_TEST_C"] == "three"
    assert os.environ["SNIPMINT_TEST_D"] == "from-env", "real environment variables win"
    assert "SNIPMINT_TEST_D" not in names
    for k in names:
        monkeypatch.delenv(k, raising=False)


def test_examples_keep_dev_and_production_apart():
    root = config.package_dir().parent
    prod = (root / ".env.example").read_text(encoding="utf-8")
    dev = (root / ".env.development.example").read_text(encoding="utf-8")
    assert "GOOGLE_CLIENT_ID=" in prod and "GOOGLE_CLIENT_ID=" in dev
    assert "localhost:5173" in dev
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert ".env.development" not in compose, "production compose must not read development settings"
    assert ".env.development" in (root / "docker-compose.dev.yml").read_text(encoding="utf-8")
