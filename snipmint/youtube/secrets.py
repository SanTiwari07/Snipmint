"""Encryption at rest for OAuth tokens (Fernet: AES-128-CBC + HMAC-SHA256).

The key comes from ``SNIPMINT_SECRET_KEY`` (a Fernet key, recommended for Docker and servers).
Without it, a random key is generated once into ``<home>/integrations/secret.key`` with
owner-only permissions. Either way the token files on disk are useless without the key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ..errors import DependencyMissing
from .errors import AuthError
from .settings import integrations_dir

INSTALL_HINT = 'Install with: pip install "snipmint[server]" (needs the cryptography package).'


class SecretError(AuthError):
    """Stored credentials could not be decrypted (the key changed or the file was edited)."""


def _fernet_cls():
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - cryptography ships with the server extra
        raise DependencyMissing("cryptography", INSTALL_HINT) from exc
    return Fernet


def available() -> bool:
    try:
        _fernet_cls()
        return True
    except DependencyMissing:
        return False


def _key_from_env(raw: str) -> bytes:
    raw = raw.strip()
    try:
        if len(base64.urlsafe_b64decode(raw.encode())) == 32:
            return raw.encode()
    except (ValueError, TypeError):
        pass
    # any other passphrase: derive a Fernet key from it
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())


def _key_file(folder: Path) -> bytes:
    path = folder / "secret.key"
    if path.is_file():
        return path.read_bytes().strip()
    key = _fernet_cls().generate_key()
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    os.replace(tmp, path)
    return key


class SecretBox:
    def __init__(self, key: bytes | None = None, folder: Path | None = None):
        env = os.environ.get("SNIPMINT_SECRET_KEY") or ""
        if key is None:
            key = _key_from_env(env) if env.strip() else _key_file(folder or integrations_dir())
        self._fernet = _fernet_cls()(key)

    def seal(self, data: dict[str, Any]) -> str:
        return self._fernet.encrypt(json.dumps(data).encode()).decode()

    def open(self, token: str) -> dict[str, Any]:
        from cryptography.fernet import InvalidToken

        try:
            return json.loads(self._fernet.decrypt(token.encode()).decode())
        except (InvalidToken, ValueError) as exc:
            raise SecretError(
                "Your saved YouTube sign-in can't be read (the secret key changed). Connect the channel again."
            ) from exc
