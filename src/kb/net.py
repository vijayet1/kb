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


def fetch(url: str, host: str, *, timeout: float, max_bytes: int, user_agent: str) -> bytes:
    """GET url over https from exactly `host`; bounded size/time; refuses redirects to other hosts."""
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or u.hostname != host:
        raise ValueError(f"refusing to fetch {url!r}: only https://{host}/ is allowed")
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})  # noqa: S310 - https + exact host validated
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - same validated request
        if urllib.parse.urlparse(r.geturl()).hostname != host:
            raise ValueError(f"redirected away from {host}; aborting")
        raw = r.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("response too large")
    return raw


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
            **(extra or {}),
        },
        "rows": rows,
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = data_dir / f"{name}.{os.getpid()}.tmp"  # unique per process: two kb runs may sync at once
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    tmp.replace(data_dir / name)
    return doc
