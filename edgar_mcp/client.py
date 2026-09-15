"""HTTP access to SEC EDGAR: rate limited, cached, and polite.

The SEC asks for two things and will block you for ignoring either: a real
User-Agent with contact details, and no more than 10 requests a second. This
module enforces both so no tool has to think about it.

The on-disk cache exists for a second reason beyond speed: it makes the eval
suite reproducible. A golden query set is worthless if the corpus shifts under it
between runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import httpx

# SEC's published ceiling is 10 requests/second. Sit under it.
MAX_REQUESTS_PER_SECOND = 8.0
CACHE_TTL_SECONDS = 24 * 3600
DEFAULT_TIMEOUT = 20.0

CACHE_DIR = Path(os.environ.get("EDGAR_MCP_CACHE", Path.home() / ".cache" / "edgar-mcp"))


class EdgarError(RuntimeError):
    """Raised with a message written for a model to act on, not just read."""


class _RateLimiter:
    """Simple spacing limiter. Thread-safe, no dependencies."""

    def __init__(self, per_second: float):
        self._min_gap = 1.0 / per_second
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self._min_gap:
                time.sleep(self._min_gap - gap)
            self._last = time.monotonic()


_limiter = _RateLimiter(MAX_REQUESTS_PER_SECOND)


def user_agent() -> str:
    ua = os.environ.get("EDGAR_MCP_USER_AGENT", "").strip()
    if not ua:
        raise EdgarError(
            "EDGAR_MCP_USER_AGENT is not set. The SEC requires a User-Agent naming "
            "who you are and how to reach you, and blocks requests without one. "
            'Set it like: EDGAR_MCP_USER_AGENT="Jane Dev jane@example.com"'
        )
    if "@" not in ua:
        raise EdgarError(
            f"EDGAR_MCP_USER_AGENT is set to {ua!r} but has no contact address. "
            'The SEC expects a name and an email, e.g. "Jane Dev jane@example.com".'
        )
    return ua


def _cache_path(url: str) -> Path:
    return CACHE_DIR / f"{hashlib.sha256(url.encode()).hexdigest()[:24]}.json"


def _read_cache(path: Path, ttl: float):
    if not path.exists():
        return None
    if ttl and time.time() - path.stat().st_mtime > ttl:
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def fetch(url: str, *, as_json: bool = True, ttl: float = CACHE_TTL_SECONDS):
    """GET a URL through the cache and the rate limiter.

    Returns parsed JSON when as_json, else the body as text.
    """
    cached = _read_cache(_cache_path(url), ttl)
    if cached is not None:
        return cached["data"]

    _limiter.wait()
    headers = {
        "User-Agent": user_agent(),
        "Accept-Encoding": "gzip, deflate",
    }
    try:
        resp = httpx.get(url, headers=headers, timeout=DEFAULT_TIMEOUT,
                         follow_redirects=True)
    except httpx.HTTPError as exc:
        raise EdgarError(f"Could not reach the SEC at {url}: {exc}") from exc

    if resp.status_code == 403:
        raise EdgarError(
            "The SEC returned 403. This almost always means the User-Agent was "
            "rejected or you exceeded the rate limit. Check EDGAR_MCP_USER_AGENT "
            "names a real person and email, then retry in a few seconds."
        )
    if resp.status_code == 404:
        raise EdgarError(f"The SEC has nothing at {url} (404).")
    if resp.status_code >= 400:
        raise EdgarError(f"SEC returned HTTP {resp.status_code} for {url}.")

    data = resp.json() if as_json else resp.text

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _cache_path(url).write_text(json.dumps({"url": url, "data": data}))
    except OSError:
        pass  # a cache write failure must never fail a request

    return data


def post_json(url: str, payload: dict, *, ttl: float = CACHE_TTL_SECONDS):
    """POST for the endpoints that need it (full-text search), cached by body."""
    key = f"{url}::{json.dumps(payload, sort_keys=True)}"
    cached = _read_cache(_cache_path(key), ttl)
    if cached is not None:
        return cached["data"]

    _limiter.wait()
    try:
        resp = httpx.post(url, json=payload, timeout=DEFAULT_TIMEOUT,
                          headers={"User-Agent": user_agent()})
    except httpx.HTTPError as exc:
        raise EdgarError(f"Could not reach the SEC at {url}: {exc}") from exc
    if resp.status_code >= 400:
        raise EdgarError(f"SEC returned HTTP {resp.status_code} for {url}.")

    data = resp.json()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _cache_path(key).write_text(json.dumps({"url": url, "data": data}))
    except OSError:
        pass
    return data


def pad_cik(cik: str | int) -> str:
    """EDGAR wants a zero-padded 10-digit CIK in most paths."""
    return str(int(str(cik).lstrip("CIK").lstrip("0") or 0)).zfill(10)
