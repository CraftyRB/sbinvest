"""Polite HTTP: per-host spacing, Retry-After and backoff.

Public endpoints (Reddit especially) rate-limit bursts, so every adaptive
request goes through here: at most one in-flight call per host, a minimum gap
between calls, and automatic retries on 429/5xx instead of a hard failure.
"""

from __future__ import annotations

import random
import threading
import time
from urllib.parse import urlparse

import requests

# Anti-bot filters reject obvious script UAs, so every request looks like a
# normal browser and carries a matching header set.
_USER_AGENTS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)
_BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}

# Minimum seconds between two requests to the same host.
_HOST_GAP = {
    "www.reddit.com": 6.0,
    "reddit.com": 6.0,
    "old.reddit.com": 6.0,
    "www.youtube.com": 1.2,
    "youtube.com": 1.2,
    "hypixel-skyblock.fandom.com": 0.6,
    "html.duckduckgo.com": 2.0,
    "duckduckgo.com": 2.0,
}
_DEFAULT_GAP = 0.5
_MAX_RETRY_WAIT = 30.0

_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}
_last_call: dict[str, float] = {}


def _lock_for(host: str) -> threading.Lock:
    with _guard:
        lock = _locks.get(host)
        if lock is None:
            lock = _locks[host] = threading.Lock()
        return lock


def _retry_after(resp: requests.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def polite_get(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 10,
    retries: int = 2,
    retry_statuses: tuple[int, ...] = (429,),
) -> requests.Response:
    """GET with per-host throttling; returns the last response when retries run out.

    `retry_statuses` lets a caller add endpoint-specific soft blocks (DuckDuckGo
    answers 202 when it wants a slower client) to the retry path.
    """
    host = urlparse(url).netloc.lower()
    gap = _HOST_GAP.get(host, _DEFAULT_GAP)
    lock = _lock_for(host)
    resp: requests.Response | None = None
    for attempt in range(retries + 1):
        with lock:
            wait = gap - (time.monotonic() - _last_call.get(host, 0.0))
            if wait > 0:
                time.sleep(wait)
            # Rotate the UA per attempt: some endpoints challenge repeat clients.
            hdrs = dict(_BROWSER_HEADERS)
            hdrs["User-Agent"] = random.choice(_USER_AGENTS)
            hdrs.update(headers or {})
            resp = requests.get(url, params=params, headers=hdrs, timeout=timeout)
            _last_call[host] = time.monotonic()

        if resp.status_code in retry_statuses and attempt < retries:
            pause = _retry_after(resp) or (2.0 * (attempt + 1) + random.random())
            time.sleep(min(_MAX_RETRY_WAIT, pause))
            continue
        if 500 <= resp.status_code < 600 and attempt < retries:
            time.sleep(1.5 * (attempt + 1))
            continue
        return resp
    assert resp is not None
    return resp
