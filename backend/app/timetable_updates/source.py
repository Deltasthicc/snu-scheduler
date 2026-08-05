"""Fetching layer: conditional HTTP requests against the public timetable
source. Confirmed by direct inspection (curl -I against the live site,
2026-08-04) that Netlify's CDN serves a real `ETag` header; no `Last-Modified`
was observed, so `If-None-Match` is the primary conditional-request
mechanism, with the source checksum as a same-effect fallback when a server
doesn't supply cache validators at all.
"""
from __future__ import annotations
import hashlib
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from app.timetable_updates.models import FetchResult

DEFAULT_USER_AGENT = "snu-scheduler-timetable-updater/1.0 (+https://github.com/; educational project)"
DEFAULT_CONNECT_TIMEOUT_S = 8.0
DEFAULT_READ_TIMEOUT_S = 20.0
DEFAULT_MAX_BYTES = 8 * 1024 * 1024  # generous relative to the ~650KB page actually observed
CHUNK_SIZE = 65536


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def fetch(
    url: str,
    *,
    known_etag: str | None = None,
    known_source_hash: str | None = None,
    force: bool = False,
    timeout_s: float = DEFAULT_READ_TIMEOUT_S,
    max_bytes: int = DEFAULT_MAX_BYTES,
    user_agent: str = DEFAULT_USER_AGENT,
) -> FetchResult:
    """One conditional fetch attempt. `force=True` bypasses the If-None-Match
    header entirely (spec: "allow a forced full fetch for diagnostics").
    Never retries internally - the poller owns retry/backoff policy, this
    function makes exactly one HTTP attempt and reports what happened."""
    retrieved_at = now_iso()
    headers = {"User-Agent": user_agent, "Accept": "text/html"}
    if known_etag and not force:
        headers["If-None-Match"] = known_etag

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            status = resp.status
            content_length = resp.headers.get("Content-Length")
            if content_length is not None and int(content_length) > max_bytes:
                return FetchResult(
                    not_modified=False, html=None, status_code=status,
                    etag=resp.headers.get("ETag"), last_modified=resp.headers.get("Last-Modified"),
                    source_hash=None, byte_length=int(content_length), retrieved_at=retrieved_at,
                    error=f"response Content-Length {content_length} exceeds the {max_bytes}-byte limit; refusing to download",
                )
            body = bytearray()
            while True:
                chunk = resp.read(CHUNK_SIZE)
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > max_bytes:
                    return FetchResult(
                        not_modified=False, html=None, status_code=status,
                        etag=resp.headers.get("ETag"), last_modified=resp.headers.get("Last-Modified"),
                        source_hash=None, byte_length=len(body), retrieved_at=retrieved_at,
                        error=f"response exceeded the {max_bytes}-byte limit while streaming; refusing to accept it",
                    )
            try:
                html = body.decode("utf-8")
            except UnicodeDecodeError as e:
                return FetchResult(
                    not_modified=False, html=None, status_code=status, etag=None, last_modified=None,
                    source_hash=None, byte_length=len(body), retrieved_at=retrieved_at,
                    error=f"response was not valid UTF-8: {e}",
                )
            source_hash = sha256_text(html)
            etag = resp.headers.get("ETag")
            # even without a server 304, a matching checksum is the same
            # effective "nothing changed" signal for servers with no cache
            # validators at all
            not_modified = bool(known_source_hash and source_hash == known_source_hash and not force)
            return FetchResult(
                not_modified=not_modified, html=None if not_modified else html, status_code=status,
                etag=etag, last_modified=resp.headers.get("Last-Modified"), source_hash=source_hash,
                byte_length=len(body), retrieved_at=retrieved_at,
            )
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return FetchResult(
                not_modified=True, html=None, status_code=304, etag=known_etag, last_modified=None,
                source_hash=known_source_hash, byte_length=None, retrieved_at=retrieved_at,
            )
        return FetchResult(
            not_modified=False, html=None, status_code=e.code, etag=None, last_modified=None,
            source_hash=None, byte_length=None, retrieved_at=retrieved_at,
            error=f"HTTP {e.code}: {e.reason}",
        )
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        return FetchResult(
            not_modified=False, html=None, status_code=None, etag=None, last_modified=None,
            source_hash=None, byte_length=None, retrieved_at=retrieved_at,
            error=f"network error: {e}",
        )
    except Exception as e:  # noqa: BLE001 - a fetch failure must never crash the poller loop
        return FetchResult(
            not_modified=False, html=None, status_code=None, etag=None, last_modified=None,
            source_hash=None, byte_length=None, retrieved_at=retrieved_at,
            error=f"unexpected fetch error: {type(e).__name__}: {e}",
        )
