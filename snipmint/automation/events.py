"""Signed events from Snipmint to n8n, through a persistent outbox.

Events are written to the store first and delivered by one background thread with
exponential backoff (5 s, 10 s, … capped at 10 min, at most ``MAX_ATTEMPTS`` tries, then marked
``dead`` and shown in the UI). If n8n is down nothing is lost and nothing in Snipmint waits.

Every request carries:
- ``X-Snipmint-Secret``: the shared secret, for n8n's Webhook node "Header Auth";
- ``X-Snipmint-Timestamp`` + ``X-Snipmint-Signature: sha256=HMAC(secret, "{ts}.{body}")`` for
  receivers that verify signatures;
- ``X-Snipmint-Delivery``: the event id, so a receiver can ignore a redelivery.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from .security import redact
from .settings import AutomationEnv
from .store import AutomationStore, OutboxEvent

log = logging.getLogger("snipmint.automation")

MAX_ATTEMPTS = 10


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def backoff(attempt: int) -> float:
    return min(600.0, 5.0 * 2 ** max(0, attempt - 1))


class EventDispatcher:
    def __init__(self, env: AutomationEnv, store: AutomationStore, http: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time):  # fmt: skip
        self.env = env
        self.store = store
        self.http = http or httpx.Client(timeout=httpx.Timeout(env.timeout, connect=5.0))
        self.clock = clock
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._health: tuple[float, bool, str | None] = (0.0, False, None)

    # ----------------------------------------------------------------- emit

    def emit(self, event_type: str, payload: dict[str, Any]) -> OutboxEvent | None:
        if not self.env.configured or not self.env.event_url(event_type):
            return None
        event = OutboxEvent(type=event_type, payload=payload, next_attempt_at=self.clock())
        self.store.save_event(event)
        self._wake.set()
        return event

    # ------------------------------------------------------------- delivery

    def _deliver(self, event: OutboxEvent) -> None:
        url = self.env.event_url(event.type)
        body = json.dumps({"id": event.id, "type": event.type, "created": event.created, "data": event.payload},
                          separators=(",", ":"), ensure_ascii=False).encode()  # fmt: skip
        ts = str(int(self.clock()))
        headers = {
            "Content-Type": "application/json",
            "X-Snipmint-Event": event.type,
            "X-Snipmint-Delivery": event.id,
            "X-Snipmint-Timestamp": ts,
            "X-Snipmint-Signature": sign(self.env.secret, ts, body),
            "X-Snipmint-Secret": self.env.secret,
        }
        event.attempts += 1
        try:
            resp = self.http.post(url or "", content=body, headers=headers)
            ok = 200 <= resp.status_code < 300
            error = None if ok else f"n8n answered {resp.status_code}"
            if resp.status_code == 404:
                error += " (is the workflow imported and active?)"
        except httpx.HTTPError as exc:
            ok, error = False, f"{type(exc).__name__}: {exc}"
        if ok:
            event.status, event.last_error = "delivered", None
        else:
            event.last_error = redact(error or "failed", self.env.secret)[:300]
            if event.attempts >= MAX_ATTEMPTS:
                event.status = "dead"
                log.warning("Gave up delivering %s %s: %s", event.type, event.id, event.last_error)
            else:
                event.next_attempt_at = self.clock() + backoff(event.attempts)
        self.store.save_event(event)

    def flush(self) -> int:
        """Deliver every event that is due now. Returns how many were attempted."""
        with self._send_lock:
            now = self.clock()
            due = [e for e in self.store.list_events() if e.status == "pending" and e.next_attempt_at <= now]
            for e in due:
                self._deliver(e)
            return len(due)

    def retry_dead(self) -> int:
        n = 0
        for e in self.store.list_events():
            if e.status == "dead":
                e.status, e.attempts, e.next_attempt_at = "pending", 0, self.clock()
                self.store.save_event(e)
                n += 1
        self._wake.set()
        return n

    def counts(self) -> dict[str, int]:
        out = {"pending": 0, "delivered": 0, "dead": 0}
        for e in self.store.list_events():
            out[e.status] += 1
        return out

    # --------------------------------------------------------------- thread

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="snipmint-n8n-events", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.flush()
            except Exception:  # never let the delivery thread die
                log.exception("n8n event delivery failed")
            self._wake.wait(5.0)
            self._wake.clear()

    # --------------------------------------------------------------- health

    def health(self, max_age: float = 30.0) -> tuple[bool, str | None]:
        """Is n8n reachable? Cached; never raises."""
        checked, ok, err = self._health
        if self.clock() - checked < max_age:
            return ok, err
        if not self.env.health_url:
            result: tuple[bool, str | None] = (False, "N8N_WEBHOOK_URL is not set.")
        else:
            try:
                resp = self.http.get(self.env.health_url, timeout=3.0)
                result = (resp.status_code < 400, None if resp.status_code < 400 else f"HTTP {resp.status_code}")
            except httpx.HTTPError as exc:
                result = (False, f"n8n is not reachable ({type(exc).__name__}).")
        self._health = (self.clock(), *result)
        return result
