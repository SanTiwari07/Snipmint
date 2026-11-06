"""Configuration for the YouTube integration. Environment variables only.

Development and production use separate settings (and should use separate OAuth clients):

- **Development** (``snipmint serve --dev``): the redirect URI may be a loopback address over
  plain http (``http://localhost:8000/api/youtube/oauth/callback``); if GOOGLE_REDIRECT_URI is
  empty, that default is used. Nothing has to be deployed.
- **Production**: GOOGLE_REDIRECT_URI must be https on your public host, or a loopback address
  for a Docker install used on the same computer. Google rejects anything else.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .. import config

UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"  # channels.list(mine=true)
SCOPES = (UPLOAD_SCOPE, READ_SCOPE)
CALLBACK_PATH = "/api/youtube/oauth/callback"


@dataclass(frozen=True)
class Endpoints:
    """Google URLs. Overridable (YOUTUBE_*_URL) so tests can point at a mock server."""

    auth: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token: str = "https://oauth2.googleapis.com/token"
    revoke: str = "https://oauth2.googleapis.com/revoke"
    api: str = "https://www.googleapis.com/youtube/v3"
    upload: str = "https://www.googleapis.com/upload/youtube/v3/videos"


@dataclass(frozen=True)
class YouTubeSettings:
    client_id: str
    client_secret: str
    redirect_uri: str
    endpoints: Endpoints = field(default_factory=Endpoints)
    concurrency: int = 2
    chunk_bytes: int = 8 * 1024 * 1024  # must be a multiple of 256 KiB
    max_retries: int = 6
    environment: str = "production"  # "development" with `serve --dev`

    @property
    def config_error(self) -> str | None:
        """Why the configuration can't work, or None."""
        if not (self.client_id and self.client_secret and self.redirect_uri):
            return None  # simply not set up
        return redirect_problem(self.redirect_uri)

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri) and self.config_error is None


def is_loopback(host: str) -> bool:
    host = host.strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def redirect_problem(uri: str) -> str | None:
    """Google's rule for web clients: https, except plain http on a loopback address."""
    try:
        parsed = urlparse(uri)
    except ValueError:
        return "GOOGLE_REDIRECT_URI is not a valid URL."
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "GOOGLE_REDIRECT_URI must start with https:// (or http://localhost for local use)."
    if parsed.scheme == "http" and not is_loopback(parsed.hostname):
        return ("GOOGLE_REDIRECT_URI must use https:// on a public host. Plain http is only allowed for "
                "localhost / 127.0.0.1 (local development or a Docker install on this computer).")  # fmt: skip
    if parsed.path.rstrip("/") != CALLBACK_PATH:
        return f"GOOGLE_REDIRECT_URI must end with {CALLBACK_PATH}."
    if parsed.query or parsed.fragment:
        return "GOOGLE_REDIRECT_URI must not have a query string or fragment."
    return None


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def dev_redirect_uri() -> str:
    """The local callback used in development when GOOGLE_REDIRECT_URI is empty."""
    port = _env("SNIPMINT_PORT") or str(config.DEFAULT_PORT)
    return f"http://localhost:{port}{CALLBACK_PATH}"


def load(dev: bool = False) -> YouTubeSettings:
    defaults = Endpoints()
    endpoints = Endpoints(
        auth=_env("YOUTUBE_AUTH_URL") or defaults.auth,
        token=_env("YOUTUBE_TOKEN_URL") or defaults.token,
        revoke=_env("YOUTUBE_REVOKE_URL") or defaults.revoke,
        api=_env("YOUTUBE_API_URL") or defaults.api,
        upload=_env("YOUTUBE_UPLOAD_URL") or defaults.upload,
    )
    try:
        concurrency = max(1, min(4, int(_env("YOUTUBE_UPLOAD_CONCURRENCY") or 2)))
    except ValueError:
        concurrency = 2
    redirect = _env("GOOGLE_REDIRECT_URI") or (dev_redirect_uri() if dev else "")
    return YouTubeSettings(
        client_id=_env("GOOGLE_CLIENT_ID"),
        client_secret=_env("GOOGLE_CLIENT_SECRET"),
        redirect_uri=redirect,
        endpoints=endpoints,
        concurrency=concurrency,
        environment="development" if dev else "production",
    )


def integrations_dir():
    """``<home>/integrations``: secrets live here, never next to projects or exports."""
    path = config.home_dir() / "integrations"
    path.mkdir(parents=True, exist_ok=True)
    return path
