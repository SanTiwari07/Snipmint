"""Resumable, checksummed file downloads for model files (NarraReel NarraReel WHAT_TO_DO.md section 5.3.3)."""

from __future__ import annotations

import hashlib
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .errors import ModelDownloadError

log = logging.getLogger("snipmint.download")

ProgressFn = Callable[[float, str], None]
_CHUNK = 1024 * 256


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def download_file(
    url: str,
    dest: Path,
    *,
    sha256: str | None = None,
    expected_size: int | None = None,
    progress: ProgressFn | None = None,
    label: str | None = None,
) -> Path:
    """Download ``url`` to ``dest`` (resuming a ``.part`` file) and verify its SHA-256.

    An existing, verified ``dest`` is returned untouched.
    """
    label = label or dest.name
    if dest.is_file() and (sha256 is None or sha256_of(dest) == sha256):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")

    for _attempt in range(2):  # second attempt restarts from scratch after a bad checksum
        have = part.stat().st_size if part.is_file() else 0
        req = urllib.request.Request(url, headers={"User-Agent": "snipmint"})
        if have:
            req.add_header("Range", f"bytes={have}-")
        try:
            resp = urllib.request.urlopen(req, timeout=30)  # noqa: S310 - https URLs we pin
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and have:  # already complete
                resp = None
            else:
                raise ModelDownloadError(
                    f"Couldn't download {label} (HTTP {exc.code}). Check your internet "
                    "connection and try again."
                ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ModelDownloadError(
                f"Couldn't download {label}: {exc}. Check your internet connection and try again."
            ) from exc

        if resp is not None:
            with resp:
                status = getattr(resp, "status", 200)
                mode = "ab" if (status == 206 and have) else "wb"
                if mode == "wb":
                    have = 0
                length = resp.headers.get("Content-Length")
                total = (int(length) + have) if length else expected_size
                done = have
                try:
                    with part.open(mode) as out:
                        while True:
                            block = resp.read(_CHUNK)
                            if not block:
                                break
                            out.write(block)
                            done += len(block)
                            if progress and total:
                                progress(min(done / total, 1.0), f"Downloading {label}")
                except OSError as exc:
                    raise ModelDownloadError(
                        f"Download of {label} was interrupted: {exc}. Run the command again to resume."
                    ) from exc
                if total and done < total:
                    raise ModelDownloadError(
                        f"Download of {label} stopped early ({done}/{total} bytes). "
                        "Run the command again to resume."
                    )

        if sha256 is None or sha256_of(part) == sha256:
            part.replace(dest)
            return dest
        log.warning("Checksum mismatch for %s; retrying from scratch", label)
        part.unlink(missing_ok=True)

    raise ModelDownloadError(
        f"{label} failed its integrity check (SHA-256 mismatch). "
        "The download may be corrupted; try again later."
    )
