"""Server-side OAuth 2.0 for a YouTube channel: authorization code + PKCE (S256) + state.

- ``start()`` makes a single-use ``state`` (expires in 10 minutes) and a PKCE verifier, both kept
  in memory on the server, plus a ``binding`` value the route sets as an HttpOnly cookie: the
  callback must come back to the same browser that started it (CSRF protection).
- ``finish()`` checks state + binding, swaps the code for tokens (with the verifier), looks up
  the channel and stores the tokens sealed with ``SecretBox``.
- ``access_token()`` refreshes shortly before expiry; ``disconnect()`` revokes at Google, then
  deletes the local copy.
Tokens never leave this module except as an ``Authorization`` header to Google.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from .client import YouTubeClient, google_error
from .errors import AuthError, NotConfigured, NotConnected, OAuthStateError, Retryable, YouTubeError
from .secrets import SecretBox
from .settings import SCOPES, YouTubeSettings
from .store import Account, Channel, IntegrationStore

log = logging.getLogger("snipmint.youtube")

STATE_TTL = 600.0
REFRESH_MARGIN = 120.0


@dataclass
class _Pending:
    verifier: str
    binding_hash: str
    created: float
    return_to: str


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class OAuthService:
    def __init__(
        self,
        settings: YouTubeSettings,
        store: IntegrationStore,
        box: SecretBox | None = None,
        client: YouTubeClient | None = None,
        clock=time.time,
    ):
        self.settings = settings
        self.store = store
        self._box = box
        self.client = client or YouTubeClient(settings)
        self.clock = clock
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()
        self._refresh_locks: dict[str, threading.Lock] = {}

    @property
    def box(self) -> SecretBox:
        if self._box is None:
            self._box = SecretBox()
        return self._box

    @property
    def http(self) -> httpx.Client:
        return self.client.http

    def _require_config(self) -> None:
        if not self.settings.configured:
            raise NotConfigured()

    # ------------------------------------------------------------------ connect

    def start(self, return_to: str = "/publish") -> tuple[str, str, str]:
        """Returns (authorization URL, state, binding cookie value)."""
        self._require_config()
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(32)
        binding = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            for key in [k for k, p in self._pending.items() if now - p.created > STATE_TTL]:
                del self._pending[key]
            self._pending[state] = _Pending(verifier, _hash(binding), now, return_to)
        params = {
            "client_id": self.settings.client_id,
            "redirect_uri": self.settings.redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
        return f"{self.settings.endpoints.auth}?{urlencode(params)}", state, binding

    def take_state(self, state: str, binding: str | None) -> _Pending:
        """Validate and consume a state (single use)."""
        with self._lock:
            pending = self._pending.pop(state or "", None)
        if pending is None:
            raise OAuthStateError("This sign-in link has expired or was already used. Start again from Snipmint.")
        if self.clock() - pending.created > STATE_TTL:
            raise OAuthStateError("The sign-in took too long. Start again from Snipmint.")
        if not binding or not hmac.compare_digest(_hash(binding), pending.binding_hash):
            raise OAuthStateError(
                "This sign-in was started in a different browser (or at a different address than "
                "GOOGLE_REDIRECT_URI). Open Snipmint at the same address and try again."
            )
        return pending

    def finish(self, state: str, code: str, binding: str | None) -> tuple[Channel, str]:
        """Complete the flow. Returns (channel, return_to)."""
        self._require_config()
        pending = self.take_state(state, binding)
        if not code:
            raise OAuthStateError("Google did not send an authorization code.")
        tokens = self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": pending.verifier,
            "redirect_uri": self.settings.redirect_uri,
        })  # fmt: skip
        if not tokens.get("refresh_token"):
            log.warning("Google returned no refresh token; uploads will stop working when the access token expires")
        granted = set(str(tokens.get("scope", "")).split())
        if granted and SCOPES[0] not in granted:
            self._revoke(tokens.get("refresh_token") or tokens["access_token"])
            raise AuthError("Snipmint needs permission to upload videos. Connect again and tick every box.")
        channel = self.client.my_channel(tokens["access_token"])
        previous = self.store.get_account(channel.id)
        if not tokens.get("refresh_token") and previous is not None:
            # Google only sends a refresh token on the first consent; keep the one we have
            tokens["refresh_token"] = self.box.open(previous.token_sealed).get("refresh_token")
        account = Account(
            **channel.model_dump(exclude={"scopes"}),
            scopes=sorted(granted) if granted else list(SCOPES),
            token_sealed=self.box.seal(self._token_record(tokens)),
        )
        self.store.save_account(account)
        log.info("Connected YouTube channel %s", channel.id)
        return account.public(), pending.return_to

    # ------------------------------------------------------------------ tokens

    def _token_record(self, tokens: dict, previous: dict | None = None) -> dict:
        prev = previous or {}
        return {
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token") or prev.get("refresh_token"),
            "expires_at": self.clock() + float(tokens.get("expires_in") or 3600),
            "scope": tokens.get("scope") or prev.get("scope", ""),
        }

    def _token_request(self, data: dict) -> dict:
        body = {**data, "client_id": self.settings.client_id, "client_secret": self.settings.client_secret}
        try:
            resp = self.http.post(self.settings.endpoints.token, data=body,
                                  headers={"Accept": "application/json"})  # fmt: skip
        except httpx.TransportError as exc:
            raise Retryable(f"Could not reach Google: {type(exc).__name__}") from None
        if resp.status_code >= 400:
            reason, message = google_error(resp)
            if reason in ("invalid_grant", "unauthorized_client", "invalid_client"):
                raise AuthError(
                    "Google refused the sign-in" + (f" ({reason})" if reason else "") +
                    ". Connect the channel again."
                )
            if resp.status_code >= 500:
                raise Retryable(f"Google sign-in is unavailable ({resp.status_code}).")
            raise YouTubeError(f"Google sign-in failed: {reason or resp.status_code} {message}".strip())
        data_out = resp.json()
        if not data_out.get("access_token"):
            raise YouTubeError("Google did not return an access token.")
        return data_out

    def _account(self, channel_id: str | None) -> Account:
        cid = channel_id or self.store.default_channel()
        account = self.store.get_account(cid) if cid else None
        if account is None:
            raise NotConnected()
        return account

    def access_token(self, channel_id: str | None = None, *, force: bool = False) -> str:
        account = self._account(channel_id)
        lock = self._refresh_locks.setdefault(account.id, threading.Lock())
        with lock:
            account = self._account(account.id)  # another thread may have refreshed meanwhile
            record = self.box.open(account.token_sealed)
            if not force and record.get("expires_at", 0) - REFRESH_MARGIN > self.clock():
                return record["access_token"]
            if not record.get("refresh_token"):
                raise AuthError("The YouTube sign-in expired. Connect the channel again.")
            self._require_config()
            tokens = self._token_request({"grant_type": "refresh_token", "refresh_token": record["refresh_token"]})
            account.token_sealed = self.box.seal(self._token_record(tokens, record))
            self.store.save_account(account)
            return tokens["access_token"]

    # --------------------------------------------------------------- disconnect

    def _revoke(self, token: str) -> bool:
        try:
            resp = self.http.post(self.settings.endpoints.revoke, data={"token": token},
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})  # fmt: skip
            return resp.status_code < 400
        except httpx.TransportError:
            return False

    def disconnect(self, channel_id: str | None = None) -> bool:
        """Revoke at Google (best effort) and delete the stored tokens. Returns whether Google confirmed."""
        account = self._account(channel_id)
        revoked = False
        try:
            record = self.box.open(account.token_sealed)
            token = record.get("refresh_token") or record.get("access_token")
            revoked = self._revoke(token) if token else False
        except YouTubeError:
            pass
        self.store.delete_account(account.id)
        log.info("Disconnected YouTube channel %s (revoked at Google: %s)", account.id, revoked)
        return revoked
