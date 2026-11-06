"""YouTube integration errors. Messages are safe to show: they never contain tokens."""

from __future__ import annotations

from ..errors import SnipmintError


class YouTubeError(SnipmintError):
    http_status = 502


class NotConfigured(YouTubeError):
    http_status = 409

    def __init__(self, message: str | None = None):
        super().__init__(
            message
            or "YouTube publishing is not set up. Add GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and "
            "GOOGLE_REDIRECT_URI to .env and restart Snipmint (see the README)."
        )


class AuthError(YouTubeError):
    """The sign-in is missing, expired or revoked: the person has to connect again."""

    http_status = 401


class OAuthStateError(YouTubeError):
    http_status = 400


class NotConnected(YouTubeError):
    http_status = 409

    def __init__(self, message: str = "Connect a YouTube channel first."):
        super().__init__(message)


class QuotaExceeded(YouTubeError):
    http_status = 429


class DuplicateUpload(YouTubeError):
    http_status = 409


class UploadRejected(YouTubeError):
    """YouTube refused the request for a reason retrying won't fix (bad metadata, forbidden)."""

    http_status = 422


class Retryable(YouTubeError):
    """A transient failure (5xx, 429 rate limit, network): retried with backoff."""
