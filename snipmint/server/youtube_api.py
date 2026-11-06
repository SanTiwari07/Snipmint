"""HTTP routes for the optional YouTube integration: /api/youtube/*.

Without GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REDIRECT_URI only ``/status`` answers
usefully (``configured: false``); everything else returns 409 with setup instructions.
No route ever returns a token.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from ..jobs.runner import JobRunner
from ..youtube import secrets as secrets_mod
from ..youtube import settings as yt_settings
from ..youtube.client import YouTubeClient
from ..youtube.errors import NotConfigured, YouTubeError
from ..youtube.oauth import STATE_TTL, OAuthService
from ..youtube.publisher import PublishManager, PublishRequest
from ..youtube.store import Channel, IntegrationStore, JsonIntegrationStore

log = logging.getLogger("snipmint.youtube")

CALLBACK_PATH = yt_settings.CALLBACK_PATH
COOKIE = "snipmint_yt_oauth"
_CLIP = re.compile(r"^clip_\d{2}\.mp4$")


class YouTubeIntegration:
    """Everything the routes need, built once per app (tests inject a mock client/store)."""

    def __init__(self, runner: JobRunner, *, store: IntegrationStore | None = None,
                 client: YouTubeClient | None = None, settings: yt_settings.YouTubeSettings | None = None,
                 dev: bool = False):  # fmt: skip
        self.settings = settings or yt_settings.load(dev=dev)
        self.runner = runner
        self._store = store
        self._client = client
        self._oauth: OAuthService | None = None
        self._manager: PublishManager | None = None

    @property
    def store(self) -> IntegrationStore:
        if self._store is None:
            self._store = JsonIntegrationStore()
        return self._store

    @property
    def oauth(self) -> OAuthService:
        if self._oauth is None:
            self._oauth = OAuthService(self.settings, self.store, client=self._client or YouTubeClient(self.settings))
        return self._oauth

    @property
    def manager(self) -> PublishManager:
        if self._manager is None:
            self._manager = PublishManager(self.settings, self.store, self.oauth, self.runner, self.resolve_clip)
        return self._manager

    def resolve_clip(self, job_id: str, clip_file: str) -> Path:
        if not _CLIP.match(clip_file):
            raise YouTubeError("Only rendered clips (clip_NN.mp4) can be published.")
        folder = self.runner.workspace_for(job_id)
        path = folder / clip_file if folder else None
        if path is None or not path.is_file():
            raise YouTubeError("That clip file is no longer there (old jobs get cleaned up). Make the clips again.")
        return path

    def startup(self) -> None:
        if self.settings.configured and secrets_mod.available():
            try:
                self.manager.resume_pending()
            except Exception:  # a broken store must not stop the app
                log.exception("Could not resume pending YouTube uploads")

    def shutdown(self) -> None:
        if self._manager is not None:
            self._manager.shutdown()


class YouTubeStatus(BaseModel):
    configured: bool
    encryption_available: bool
    environment: str = "production"
    config_error: str | None = Field(None, description="Why the Google settings can't work (e.g. a non-https redirect URI)")
    redirect_uri: str | None = None
    channels: list[Channel] = Field(default_factory=list)
    default_channel: str | None = None
    concurrency: int = 2
    scopes: list[str] = Field(default_factory=list)


class OAuthStart(BaseModel):
    return_to: str = Field("/publish", max_length=200)


class OAuthStartResponse(BaseModel):
    auth_url: str


class ChannelRef(BaseModel):
    channel_id: str | None = None


class DisconnectResponse(BaseModel):
    disconnected: bool
    revoked: bool


class PublishCreated(BaseModel):
    batch_id: str
    task_id: str
    items: list[dict[str, Any]]


def _safe_return(path: str) -> str:
    """Only same-site paths: no scheme, no host, no protocol-relative //."""
    if not path.startswith("/") or path.startswith("//") or "\\" in path or "://" in path:
        return "/publish"
    return path


def ui_base(dev: bool) -> str:
    env = (os.environ.get("SNIPMINT_UI_URL") or "").strip().rstrip("/")
    if env:
        return env
    return "http://localhost:5173" if dev else ""


def register(app: FastAPI, runner: JobRunner, *, dev: bool = False,
             integration: YouTubeIntegration | None = None) -> YouTubeIntegration:  # fmt: skip
    yt = integration or YouTubeIntegration(runner, dev=dev)
    app.state.youtube = yt

    def need_config() -> None:
        if yt.settings.config_error:
            raise NotConfigured(yt.settings.config_error)
        if not yt.settings.configured:
            raise NotConfigured()
        if not secrets_mod.available():
            raise NotConfigured('Token encryption needs the cryptography package: pip install "snipmint[server]".')

    @app.get("/api/youtube/status", response_model=YouTubeStatus, tags=["youtube"])
    def status() -> YouTubeStatus:
        ok = yt.settings.configured
        enc = secrets_mod.available()
        channels = [a.public() for a in yt.store.list_accounts()] if ok and enc else []
        return YouTubeStatus(
            configured=ok, encryption_available=enc,
            environment=yt.settings.environment, config_error=yt.settings.config_error,
            redirect_uri=yt.settings.redirect_uri or None,
            channels=channels, default_channel=yt.store.default_channel() if channels else None,
            concurrency=yt.settings.concurrency, scopes=list(yt_settings.SCOPES),
        )  # fmt: skip

    @app.post("/api/youtube/oauth/start", response_model=OAuthStartResponse, tags=["youtube"])
    def oauth_start(body: OAuthStart) -> Any:
        need_config()
        url, _state, binding = yt.oauth.start(_safe_return(body.return_to))
        from fastapi.responses import JSONResponse

        resp = JSONResponse(OAuthStartResponse(auth_url=url).model_dump())
        resp.set_cookie(COOKIE, binding, max_age=int(STATE_TTL), httponly=True, samesite="lax",
                        secure=yt.settings.redirect_uri.startswith("https://"), path="/api/youtube/oauth")  # fmt: skip
        return resp

    @app.get(CALLBACK_PATH, tags=["youtube"], include_in_schema=False)
    async def oauth_callback(request: Request, state: str = "", code: str = "", error: str = "") -> RedirectResponse:
        """Google redirects the browser here. Protected by state + browser cookie + PKCE, not the API token."""
        base = ui_base(dev)
        binding = request.cookies.get(COOKIE)
        if error:
            # the person pressed "Cancel" on Google's screen, or Google refused
            try:
                pending = yt.oauth.take_state(state, binding)
                target = pending.return_to
            except YouTubeError:
                target = "/publish"
            msg = "You cancelled the sign-in." if error == "access_denied" else f"Google returned: {error[:80]}"
            resp = RedirectResponse(f"{base}{target}?{urlencode({'youtube': 'error', 'message': msg})}", 303)
        else:
            try:
                need_config()
                channel, target = await asyncio.to_thread(yt.oauth.finish, state, code, binding)
                query = urlencode({"youtube": "connected", "channel": channel.title[:80]})
                resp = RedirectResponse(f"{base}{target}?{query}", 303)
            except YouTubeError as exc:
                log.info("YouTube sign-in failed: %s", exc)
                resp = RedirectResponse(f"{base}/publish?{urlencode({'youtube': 'error', 'message': str(exc)[:300]})}", 303)
        resp.delete_cookie(COOKIE, path="/api/youtube/oauth")
        return resp

    @app.post("/api/youtube/disconnect", response_model=DisconnectResponse, tags=["youtube"])
    async def disconnect(body: ChannelRef) -> DisconnectResponse:
        need_config()
        revoked = await asyncio.to_thread(yt.oauth.disconnect, body.channel_id)
        return DisconnectResponse(disconnected=True, revoked=revoked)

    @app.post("/api/youtube/channels/{channel_id}/default", response_model=YouTubeStatus, tags=["youtube"])
    def set_default(channel_id: str) -> YouTubeStatus:
        need_config()
        if yt.store.get_account(channel_id) is None:
            raise YouTubeError("That channel isn't connected.")
        yt.store.set_default_channel(channel_id)
        return status()

    @app.post("/api/youtube/publish", response_model=PublishCreated, tags=["youtube"])
    async def publish(req: PublishRequest) -> PublishCreated:
        need_config()
        batch_id, items = await asyncio.to_thread(yt.manager.submit, req)
        return PublishCreated(batch_id=batch_id, task_id=batch_id, items=[i.public() for i in items])

    @app.get("/api/youtube/publish", tags=["youtube"])
    def list_published(scheduled: bool | None = None, limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
        if not yt.settings.configured:
            return []
        items = yt.store.list_items()
        if scheduled is not None:
            items = [i for i in items if i.scheduled == scheduled]
        return [i.public() for i in items[:limit]]

    @app.get("/api/youtube/scheduled", tags=["youtube"])
    def list_scheduled() -> list[dict[str, Any]]:
        """Scheduled uploads, soonest first."""
        if not yt.settings.configured:
            return []
        items = [i for i in yt.store.list_items() if i.scheduled and i.status != "CANCELLED"]
        return [i.public() for i in sorted(items, key=lambda i: i.details.publish_at or "")]

    @app.get("/api/youtube/publish/{item_id}", tags=["youtube"])
    def get_item(item_id: str) -> dict[str, Any]:
        need_config()
        return yt.manager._item(item_id).public()

    @app.post("/api/youtube/publish/{item_id}/cancel", tags=["youtube"])
    def cancel_item(item_id: str) -> dict[str, Any]:
        need_config()
        return yt.manager.cancel(item_id).public()

    @app.post("/api/youtube/publish/{item_id}/retry", response_model=PublishCreated, tags=["youtube"])
    def retry_item(item_id: str) -> PublishCreated:
        need_config()
        item = yt.manager.retry(item_id)
        return PublishCreated(batch_id=item.batch_id, task_id=item.batch_id, items=[item.public()])

    @app.delete("/api/youtube/publish/{item_id}", tags=["youtube"])
    def remove_item(item_id: str) -> dict[str, bool]:
        need_config()
        yt.manager.remove(item_id)
        return {"removed": True}

    return yt
