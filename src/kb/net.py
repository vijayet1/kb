"""Network and downloaded-data-file code. Nothing else in kb may import the network stack (see tests)."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

NETWORK_ERRORS: tuple[type[BaseException], ...] = (OSError, ValueError, http.client.HTTPException)


def xdg_dir(var: str, fallback: Path) -> Path:
    value = os.environ.get(var, "")
    return Path(value) if value and Path(value).is_absolute() else fallback


def write_private(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)


class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only if it stays on https and on the allowed host (checked before the next request)."""

    def __init__(self, host: str):
        self.host = host

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urllib.parse.urlparse(newurl)
        if u.scheme != "https" or u.hostname != self.host:
            raise ValueError(f"refusing redirect to {newurl!r}: only https://{self.host}/ is allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, host: str, *, timeout: float, max_bytes: int, user_agent: str, opener=None) -> bytes:
    """GET url over https from exactly `host`; redirects stay on that host; bounded size and total time.

    The total deadline is checked between chunks and each socket operation is bounded by `timeout`, so the worst
    case is a small multiple of `timeout`.
    """
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or u.hostname != host:
        raise ValueError(f"refusing to fetch {url!r}: only https://{host}/ is allowed")
    if opener is None:
        opener = urllib.request.build_opener(SameHostRedirect(host))
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})  # noqa: S310 - https + exact host validated
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    total = 0
    with opener.open(req, timeout=timeout) as r:
        while True:
            if time.monotonic() > deadline:
                raise ValueError(f"download from {host} exceeded its {timeout:g}s deadline")
            chunk = r.read(min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("response too large")
    return b"".join(chunks)


def save_doc(
    data_dir: Path,
    name: str,
    url: str,
    raw: bytes,
    rows: list[dict],
    min_rows: int,
    *,
    version: str,
    extra: dict | None = None,
) -> dict:
    if len(rows) < min_rows:
        raise ValueError(f"only {len(rows)} rows parsed - page layout probably changed; keeping old data")
    doc = {
        "meta": {
            "source": url,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "kb_version": version,
            "rows": len(rows),
            "schema": 1,
            "rows_sha256": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
            **(extra or {}),
        },
        "rows": rows,
    }
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = data_dir / f"{name}.{os.getpid()}.tmp"  # unique per process: two kb runs may sync at once
    try:
        write_private(tmp, json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
        tmp.replace(data_dir / name)
    finally:
        tmp.unlink(missing_ok=True)
    return doc
