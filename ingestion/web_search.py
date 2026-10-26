"""Keyless web search via DuckDuckGo's HTML endpoint.

DuckDuckGo indexes Reddit, YouTube, forums and wikis, so it fills the gaps left
by the native APIs without an API key or a hard rate limit. Requests go through
the polite HTTP layer (per-host spacing + backoff).
"""

from __future__ import annotations

import hashlib
import re
import time
from urllib.parse import parse_qs, unquote, urlparse

from ingestion.feed_utils import clean_html
from ingestion.http import polite_get

SEARCH_URL = "https://duckduckgo.com/html/"

# DuckDuckGo answers bot traffic with a 202 "anomaly" page and then a hard 403.
# Hammering it escalates the block, so after one challenge the whole process
# stops calling it for a while instead of retrying every query.
BLOCK_COOLDOWN_SECONDS = 900.0
_blocked_until = 0.0


class WebSearchBlocked(RuntimeError):
    """Raised while DuckDuckGo is refusing requests from this network."""


_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
    r'.*?(?:<a[^>]+class="result__snippet"[^>]*>(.*?)</a>)?',
    re.S | re.I,
)


def _target(link: str) -> str:
    """Unwrap DuckDuckGo's /l/?uddg=<encoded-url> redirect."""
    if "uddg=" in link:
        query = parse_qs(urlparse(link).query)
        if query.get("uddg"):
            return unquote(query["uddg"][0])
    return link


def search_web(query: str, limit: int = 4) -> list[dict]:
    global _blocked_until
    if time.monotonic() < _blocked_until:
        raise WebSearchBlocked("DuckDuckGo cooldown active — web evidence skipped")

    resp = polite_get(
        SEARCH_URL,
        params={"q": query, "kl": "us-en"},
        timeout=10,
        retries=1,
        retry_statuses=(429, 503),
    )
    if resp.status_code in (202, 403):
        _blocked_until = time.monotonic() + BLOCK_COOLDOWN_SECONDS
        raise WebSearchBlocked(
            f"DuckDuckGo is blocking this network (HTTP {resp.status_code}) — web evidence skipped"
        )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} for DuckDuckGo")
    html = resp.text

    results: list[dict] = []
    seen: set[str] = set()
    seen_pages: set[tuple[str, str]] = set()
    for link, title, snippet in _RESULT_RE.findall(html):
        url = _target(link)
        if not url.startswith("http") or url in seen:
            continue
        clean_title = clean_html(title, 200)
        # DuckDuckGo often returns the same wiki page under several URLs; keep
        # one hit per (domain, title) so the pool stays diverse.
        page = (urlparse(url).netloc.lower(), re.sub(r"\W+", " ", clean_title.lower()).strip())
        if page in seen_pages:
            continue
        seen.add(url)
        seen_pages.add(page)
        results.append(
            {
                "result_id": hashlib.sha1(url.encode("utf-8")).hexdigest()[:16],
                "title": clean_title,
                "url": url,
                "snippet": clean_html(snippet, 800),
                # DuckDuckGo exposes no publication date; leave it blank so the
                # recency filter does not pretend it is fresh.
                "published": "",
            }
        )
        if len(results) >= limit:
            break
    if not results and "anomaly" in html.lower():
        _blocked_until = time.monotonic() + BLOCK_COOLDOWN_SECONDS
        raise WebSearchBlocked("DuckDuckGo served a bot challenge — web evidence skipped")
    return results

