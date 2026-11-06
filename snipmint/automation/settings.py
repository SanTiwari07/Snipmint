"""Environment configuration for the n8n integration (secrets never come from the UI)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

# event type -> n8n webhook path (one workflow per path; see workflows/n8n/)
EVENT_PATHS: dict[str, str] = {
    "clip.ready": "snipmint/clip-ready",
    "batch.created": "snipmint/batch",
    "metadata.requested": "snipmint/metadata",
    "metadata.awaiting_approval": "snipmint/notify",
    "upload.succeeded": "snipmint/notify",
    "upload.failed": "snipmint/notify",
    "schedule.triggered": "snipmint/notify",
    "batch.completed": "snipmint/notify",
}
MIN_SECRET_LENGTH = 24


@dataclass(frozen=True)
class AutomationEnv:
    secret: str
    webhook_url: str  # base of n8n's production webhooks, e.g. http://n8n:5678/webhook
    health_url: str
    timeout: float = 10.0
    rate_per_minute: int = 120

    @property
    def configured(self) -> bool:
        return bool(self.secret) and self.config_error is None

    @property
    def config_error(self) -> str | None:
        if not self.secret:
            return None
        if len(self.secret) < MIN_SECRET_LENGTH:
            return f"SNIPMINT_N8N_WEBHOOK_SECRET must be at least {MIN_SECRET_LENGTH} characters."
        if self.webhook_url:
            p = urlparse(self.webhook_url)
            if p.scheme not in ("http", "https") or not p.hostname:
                return "N8N_WEBHOOK_URL must be an http(s) URL, e.g. http://n8n:5678/webhook."
        return None

    def event_url(self, event_type: str) -> str | None:
        path = EVENT_PATHS.get(event_type)
        if not path or not self.webhook_url:
            return None
        return f"{self.webhook_url.rstrip('/')}/{path}"


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def load() -> AutomationEnv:
    hook = _env("N8N_WEBHOOK_URL")
    health = _env("N8N_HEALTH_URL")
    if not health and hook:
        p = urlparse(hook)
        health = f"{p.scheme}://{p.netloc}/healthz" if p.scheme and p.netloc else ""
    try:
        rate = max(10, int(_env("SNIPMINT_N8N_RATE_PER_MINUTE") or 120))
    except ValueError:
        rate = 120
    return AutomationEnv(secret=_env("SNIPMINT_N8N_WEBHOOK_SECRET"), webhook_url=hook, health_url=health,
                         rate_per_minute=rate)  # fmt: skip
