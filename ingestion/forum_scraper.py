"""Hypixel forum RSS scraper (SkyBlock patch notes, Alpha network, discussion).

Hypixel serves valid RSS at /forums/<slug>/index.rss. A browser-like User-Agent
is required; otherwise Cloudflare returns 403.
"""

from __future__ import annotations

import requests

import config
import database
from ingestion.feed_utils import clean_html, published_iso

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _fetch_feed(url: str) -> list[dict]:
    import feedparser

    resp = requests.get(
        url,
        headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml"},
        timeout=config.SOURCE_REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} for {url}")
    parsed = feedparser.parse(resp.content)
    feed_title = (parsed.feed or {}).get("title", url)
    posts = []
    for entry in parsed.entries[:10]:
        link = entry.get("link") or ""
        uid = entry.get("id") or link
        if not uid:
            continue
        posts.append(
            {
                "uid": uid,
                "title": clean_html(entry.get("title"), limit=300),
                "link": link,
                "published": published_iso(entry),
                "author": entry.get("author") or "",
                "summary": clean_html(entry.get("summary") or entry.get("description"), 2500),
                "feed": feed_title,
            }
        )
    return posts


def collect_forum_posts(per_feed: int = 10) -> dict:
    """Fetch every configured feed, persist new posts, return the unseen ones."""
    items: list[dict] = []
    errors: list[str] = []
    for url in config.FORUM_FEEDS:
        try:
            posts = _fetch_feed(url)[:per_feed]
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            continue
        new = [p for p in posts if not database.saw_forum_post(p["uid"])]
        if new:
            database.save_forum_posts(new)
            items.extend(new)
    return {"items": items, "errors": errors}
