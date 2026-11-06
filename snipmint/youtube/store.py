"""Persistent state for the YouTube integration, behind a small storage interface.

``IntegrationStore`` is what the rest of the code uses. ``JsonIntegrationStore`` keeps two JSON
files under ``<home>/integrations/youtube/`` with atomic writes (temp file + replace); a SQLite
or Postgres store only has to implement the same methods.

Accounts hold the channel's public details plus its OAuth tokens *sealed* by ``SecretBox``.
Nothing in here is ever copied into projects, job folders or exports.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from .settings import integrations_dir

PublishStatus = Literal["QUEUED", "PREPARING", "UPLOADING", "PUBLISHED", "FAILED", "CANCELLED"]
ACTIVE: tuple[str, ...] = ("QUEUED", "PREPARING", "UPLOADING")
Privacy = Literal["private", "unlisted", "public"]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Channel(BaseModel):
    """What the UI may see about a connected channel."""

    id: str
    title: str = ""
    custom_url: str | None = None
    thumbnail: str | None = None
    connected_at: str = Field(default_factory=now_iso)
    scopes: list[str] = Field(default_factory=list)


class Account(Channel):
    token_sealed: str  # encrypted {access_token, refresh_token, expires_at, scope}

    def public(self) -> Channel:
        return Channel.model_validate(self.model_dump(exclude={"token_sealed"}))


class VideoDetails(BaseModel):
    """What is sent to videos.insert (snippet + status)."""

    title: str = Field(min_length=1, max_length=100)
    description: str = Field("", max_length=5000)
    tags: list[str] = Field(default_factory=list)
    category_id: str = "22"
    privacy: Privacy = "private"
    made_for_kids: bool = False
    language: str | None = None
    publish_at: str | None = None  # RFC 3339 UTC; forces privacy=private until then


class PublishItem(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    batch_id: str = ""
    channel_id: str
    job_id: str
    clip_file: str
    clip_id: str | None = None
    source_id: str | None = None
    file_sha256: str
    file_size: int
    details: VideoDetails
    status: PublishStatus = "QUEUED"
    progress: float = 0.0
    bytes_sent: int = 0
    attempts: int = 0
    video_id: str | None = None
    video_url: str | None = None
    error: str | None = None
    error_kind: str | None = None  # exception class, so callers can tell transient from permanent failures
    note: str | None = None
    created: float = Field(default_factory=time.time)
    updated: float = Field(default_factory=time.time)
    # resumable session URL: lets an interrupted upload continue after a restart. Never public.
    upload_url: str | None = None

    @property
    def scheduled(self) -> bool:
        return bool(self.details.publish_at)

    def public(self) -> dict:
        data = self.model_dump(exclude={"upload_url"})
        data["scheduled"] = self.scheduled
        return data


class IntegrationStore(Protocol):
    def list_accounts(self) -> list[Account]: ...
    def get_account(self, channel_id: str) -> Account | None: ...
    def save_account(self, account: Account) -> None: ...
    def delete_account(self, channel_id: str) -> bool: ...
    def default_channel(self) -> str | None: ...
    def set_default_channel(self, channel_id: str | None) -> None: ...

    def list_items(self) -> list[PublishItem]: ...
    def get_item(self, item_id: str) -> PublishItem | None: ...
    def save_item(self, item: PublishItem) -> None: ...
    def delete_item(self, item_id: str) -> bool: ...


def _atomic_write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: a reader (antivirus) holds the file for a moment
            if attempt == 19:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.025)


def _read(path: Path, default: dict) -> dict:
    for attempt in range(20):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return dict(default)
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.025)
        except ValueError:  # a corrupt file must not take the app down
            return dict(default)
    return dict(default)


class JsonIntegrationStore:
    """File-backed store. Safe across threads in one process."""

    def __init__(self, folder: Path | None = None):
        self.folder = Path(folder) if folder else integrations_dir() / "youtube"
        self.folder.mkdir(parents=True, exist_ok=True)
        self._accounts = self.folder / "accounts.json"
        self._items = self.folder / "publish.json"
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- accounts

    def _load_accounts(self) -> dict:
        return _read(self._accounts, {"default": None, "channels": {}})

    def list_accounts(self) -> list[Account]:
        with self._lock:
            data = self._load_accounts()
            return [Account.model_validate(a) for a in data.get("channels", {}).values()]

    def get_account(self, channel_id: str) -> Account | None:
        with self._lock:
            raw = self._load_accounts().get("channels", {}).get(channel_id)
            return Account.model_validate(raw) if raw else None

    def save_account(self, account: Account) -> None:
        with self._lock:
            data = self._load_accounts()
            data.setdefault("channels", {})[account.id] = account.model_dump()
            if not data.get("default"):
                data["default"] = account.id
            _atomic_write(self._accounts, data)

    def delete_account(self, channel_id: str) -> bool:
        with self._lock:
            data = self._load_accounts()
            gone = data.get("channels", {}).pop(channel_id, None) is not None
            if data.get("default") == channel_id:
                data["default"] = next(iter(data["channels"]), None)
            _atomic_write(self._accounts, data)
            return gone

    def default_channel(self) -> str | None:
        with self._lock:
            return self._load_accounts().get("default")

    def set_default_channel(self, channel_id: str | None) -> None:
        with self._lock:
            data = self._load_accounts()
            data["default"] = channel_id
            _atomic_write(self._accounts, data)

    # ------------------------------------------------------------------- items

    def _load_items(self) -> dict:
        return _read(self._items, {"items": {}})

    def list_items(self) -> list[PublishItem]:
        with self._lock:
            items = [PublishItem.model_validate(v) for v in self._load_items().get("items", {}).values()]
        return sorted(items, key=lambda i: i.created, reverse=True)

    def get_item(self, item_id: str) -> PublishItem | None:
        with self._lock:
            raw = self._load_items().get("items", {}).get(item_id)
            return PublishItem.model_validate(raw) if raw else None

    def save_item(self, item: PublishItem) -> None:
        with self._lock:
            data = self._load_items()
            item.updated = time.time()
            data.setdefault("items", {})[item.id] = item.model_dump()
            _atomic_write(self._items, data)

    def delete_item(self, item_id: str) -> bool:
        with self._lock:
            data = self._load_items()
            gone = data.get("items", {}).pop(item_id, None) is not None
            _atomic_write(self._items, data)
            return gone
