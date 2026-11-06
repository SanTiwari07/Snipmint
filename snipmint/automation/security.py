"""Authentication, rate limiting and secret redaction for the n8n endpoints."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import threading
import time

SIGNATURE_MAX_AGE = 300  # seconds a signed request stays valid
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_GOOGLE = re.compile(r"(ya29\.[A-Za-z0-9._-]+|1//[A-Za-z0-9._-]+|AIza[0-9A-Za-z_\-]{20,})")


def redact(text: str, *secrets: str) -> str:
    """Remove known secrets, bearer tokens and Google tokens from a string."""
    out = _GOOGLE.sub("[redacted]", _BEARER.sub(r"\1[redacted]", text))
    for s in secrets:
        if s and len(s) >= 6:
            out = out.replace(s, "[redacted]")
    return out


class RedactingFilter:
    """Redacts every log record in the process (all loggers, all handlers) while installed.

    A filter on one logger would miss records from its child loggers, so this wraps the
    log-record factory instead."""

    def __init__(self, *secrets: str):
        self.secrets = [s for s in secrets if s]
        self._previous = None

    def install(self) -> None:
        if self._previous is not None:
            return
        previous = logging.getLogRecordFactory()
        secrets = self.secrets

        def factory(*args, **kwargs):
            record = previous(*args, **kwargs)
            try:
                msg = record.getMessage()
            except Exception:
                return record
            clean = redact(msg, *secrets)
            if clean != msg:
                record.msg, record.args = clean, ()
            return record

        self._previous = previous
        logging.setLogRecordFactory(factory)

    def uninstall(self) -> None:
        if self._previous is not None:
            logging.setLogRecordFactory(self._previous)
            self._previous = None


def verify_request(secret: str, headers, body: bytes, now: float | None = None) -> str | None:
    """None if the request is authentic, else the reason. Accepts either the shared secret in
    ``X-Snipmint-Secret`` (n8n Header Auth credential) or an HMAC signature with a fresh timestamp."""
    if not secret:
        return "n8n integration is not configured."
    given = headers.get("x-snipmint-secret")
    if given is not None:
        return None if hmac.compare_digest(given.encode(), secret.encode()) else "Invalid credentials."
    sig, ts = headers.get("x-snipmint-signature"), headers.get("x-snipmint-timestamp")
    if sig and ts:
        try:
            age = abs((now or time.time()) - int(ts))
        except ValueError:
            return "Invalid credentials."
        if age > SIGNATURE_MAX_AGE:
            return "Signature expired."
        mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        return None if hmac.compare_digest(sig.encode(), f"sha256={mac}".encode()) else "Invalid credentials."
    return "Missing credentials (X-Snipmint-Secret or X-Snipmint-Signature)."


class RateLimiter:
    """Token bucket per client: ``rate`` requests a minute, bursts up to twice that."""

    def __init__(self, rate_per_minute: int, clock=time.monotonic):
        self.rate = rate_per_minute / 60.0
        self.capacity = float(rate_per_minute * 2)
        self.clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens < 1:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1, now)
            return True
