"""A fake Google: OAuth (with PKCE checks), channels.list and the resumable upload protocol.

``MockGoogle.handle`` answers ``httpx.Request`` objects, so the same fake backs an
``httpx.MockTransport`` (unit tests) and a real HTTP server on localhost (integration test).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx

from snipmint.youtube.settings import Endpoints, YouTubeSettings

CLIENT_ID = "test-client.apps.googleusercontent.com"
CLIENT_SECRET = "test-secret-value"
REDIRECT = "http://localhost/api/youtube/oauth/callback"


def endpoints(base: str) -> Endpoints:
    return Endpoints(auth=f"{base}/o/oauth2/v2/auth", token=f"{base}/token", revoke=f"{base}/revoke",
                     api=f"{base}/youtube/v3", upload=f"{base}/upload/youtube/v3/videos")  # fmt: skip


def make_settings(base: str = "https://mock.google", **kw) -> YouTubeSettings:
    return YouTubeSettings(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, redirect_uri=REDIRECT,
                           endpoints=endpoints(base), **kw)  # fmt: skip


@dataclass
class Session:
    size: int
    body: dict
    data: bytearray = field(default_factory=bytearray)


class MockGoogle:
    def __init__(self, base: str = "https://mock.google"):
        self.base = base
        self.lock = threading.Lock()
        self.codes: dict[str, dict] = {}
        self.access: dict[str, str] = {}  # token -> refresh token it came from
        self.refresh: set[str] = set()
        self.revoked: list[str] = []
        self.sessions: dict[str, Session] = {}
        self.videos: dict[str, dict] = {}
        self.chunk_failures: list[int] = []  # status codes for the next chunk PUTs
        self.session_failures: list[int] = []
        self.expire_once = False  # next upload PUT answers 401 once
        self.force_private = False  # simulate an unverified app
        self.quota_exceeded = False
        self.requests: list[tuple[str, str]] = []
        self.channel = {"id": "UC_test_channel", "snippet": {"title": "Test Channel", "customUrl": "@test",
                        "thumbnails": {"default": {"url": "https://yt3.example/t.jpg"}}}}  # fmt: skip
        self.scope = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"

    # ---------------------------------------------------------------- consent

    def consent(self, auth_url: str) -> tuple[str, str]:
        """What Google's consent screen does: returns (code, state) for the redirect."""
        q = {k: v[0] for k, v in parse_qs(urlparse(auth_url).query).items()}
        assert q["client_id"] == CLIENT_ID
        assert q["code_challenge_method"] == "S256"
        assert q["response_type"] == "code" and q["access_type"] == "offline"
        code = secrets.token_urlsafe(12)
        self.codes[code] = {"challenge": q["code_challenge"], "redirect_uri": q["redirect_uri"]}
        return code, q["state"]

    # ---------------------------------------------------------------- handler

    def _json(self, status: int, body: dict, headers: dict | None = None) -> httpx.Response:
        return httpx.Response(status, json=body, headers=headers or {})

    def _authed(self, request: httpx.Request) -> bool:
        auth = request.headers.get("authorization", "")
        return auth.startswith("Bearer ") and auth[7:] in self.access

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        path = url.path
        with self.lock:
            self.requests.append((request.method, path))
            if path == "/token":
                return self._token(dict(parse_qs(request.content.decode())))
            if path == "/revoke":
                token = parse_qs(request.content.decode()).get("token", [""])[0]
                self.revoked.append(token)
                self.refresh.discard(token)
                self.access = {a: r for a, r in self.access.items() if a != token and r != token}
                return httpx.Response(200)
            if path == "/youtube/v3/channels":
                if not self._authed(request):
                    return self._json(401, {"error": {"code": 401, "message": "Invalid Credentials"}})
                return self._json(200, {"items": [self.channel]})
            if path == "/upload/youtube/v3/videos" and request.method == "POST":
                return self._start(request)
            if path.startswith("/upload/session/") and request.method == "PUT":
                return self._chunk(request, path.rsplit("/", 1)[1])
        return self._json(404, {"error": {"code": 404, "message": f"no route {path}"}})

    def _token(self, form: dict) -> httpx.Response:
        f = {k: v[0] for k, v in form.items()}
        if f.get("client_id") != CLIENT_ID or f.get("client_secret") != CLIENT_SECRET:
            return self._json(401, {"error": "invalid_client", "error_description": "bad client"})
        if f.get("grant_type") == "authorization_code":
            info = self.codes.pop(f.get("code", ""), None)
            if info is None:
                return self._json(400, {"error": "invalid_grant", "error_description": "bad code"})
            digest = base64.urlsafe_b64encode(hashlib.sha256(f.get("code_verifier", "").encode()).digest())
            if digest.rstrip(b"=").decode() != info["challenge"]:
                return self._json(400, {"error": "invalid_grant", "error_description": "PKCE verification failed"})
            if f.get("redirect_uri") != info["redirect_uri"]:
                return self._json(400, {"error": "redirect_uri_mismatch"})
            refresh = "1//refresh-" + secrets.token_urlsafe(8)
            self.refresh.add(refresh)
        elif f.get("grant_type") == "refresh_token":
            refresh = f.get("refresh_token", "")
            if refresh not in self.refresh:
                return self._json(400, {"error": "invalid_grant", "error_description": "Token has been revoked."})
        else:
            return self._json(400, {"error": "unsupported_grant_type"})
        access = "ya29.access-" + secrets.token_urlsafe(8)
        self.access[access] = refresh
        body = {"access_token": access, "expires_in": 3599, "scope": self.scope, "token_type": "Bearer"}
        if f.get("grant_type") == "authorization_code":
            body["refresh_token"] = refresh
        return self._json(200, body)

    def _start(self, request: httpx.Request) -> httpx.Response:
        if not self._authed(request):
            return self._json(401, {"error": {"code": 401, "message": "Invalid Credentials"}})
        if self.quota_exceeded:
            return self._json(403, {"error": {"code": 403, "message": "quota", "errors": [{"reason": "quotaExceeded"}]}})
        if self.session_failures:
            return self._json(self.session_failures.pop(0), {"error": {"code": 503, "message": "busy"}})
        assert request.url.params.get("uploadType") == "resumable"
        body = json.loads(request.content)
        assert body["snippet"]["title"]
        sid = secrets.token_hex(6)
        self.sessions[sid] = Session(size=int(request.headers["x-upload-content-length"]), body=body)
        return httpx.Response(200, headers={"Location": f"{self.base}/upload/session/{sid}"})

    def _chunk(self, request: httpx.Request, sid: str) -> httpx.Response:
        sess = self.sessions.get(sid)
        if sess is None:
            return self._json(404, {"error": {"code": 404, "message": "session gone"}})
        if self.expire_once:
            self.expire_once = False
            self.access.clear()
        if not self._authed(request):
            return self._json(401, {"error": {"code": 401, "message": "Invalid Credentials"}})
        rng = request.headers.get("content-range", "")
        if rng.startswith("bytes */"):
            return self._status(sess, sid)
        if self.chunk_failures:
            return self._json(self.chunk_failures.pop(0), {"error": {"code": 503, "message": "backend error"}})
        first = int(rng.split(" ")[1].split("-")[0])
        if first != len(sess.data):
            return self._status(sess, sid)
        sess.data.extend(request.content)
        return self._status(sess, sid)

    def _status(self, sess: Session, sid: str) -> httpx.Response:
        if len(sess.data) >= sess.size:
            vid = "vid" + sid
            status = dict(sess.body["status"])
            if self.force_private:
                status["privacyStatus"] = "private"
            video = {"id": vid, "snippet": sess.body["snippet"], "status": {**status, "uploadStatus": "uploaded"}}
            self.videos[vid] = {**video, "bytes": bytes(sess.data)}
            return self._json(200, video)
        headers = {"Range": f"bytes=0-{len(sess.data) - 1}"} if sess.data else {}
        return httpx.Response(308, headers=headers)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


class MockGoogleServer:
    """Serve a MockGoogle over real HTTP on 127.0.0.1 (random port)."""

    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.google = MockGoogle(self.base)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _do(self):
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                req = httpx.Request(self.command, f"{outer.base}{self.path}", headers=dict(self.headers.items()),
                                    content=body)  # fmt: skip
                resp = outer.google.handle(req)
                self.send_response(resp.status_code)
                for k, v in resp.headers.items():
                    if k.lower() not in ("content-length", "transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(resp.content)))
                self.end_headers()
                self.wfile.write(resp.content)

            do_GET = do_POST = do_PUT = _do

            def log_message(self, *args):  # keep test output clean
                pass

        return Handler

    def __enter__(self) -> MockGoogleServer:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
