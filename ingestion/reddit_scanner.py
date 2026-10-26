"""r/HypixelSkyblock scanner: new.json, then RSS, then the Arctic Shift mirror.

Reddit frequently 403s/429s unauthenticated JSON from datacenter IPs, while the
public .rss endpoint sometimes works. When both are throttled we read the same
subreddit through the keyless Arctic Shift archive, which also supports a title
search so per-item evidence still flows. All paths normalize to one shape.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import config
import database
from ingestion.feed_utils import clean_html, published_iso
from ingestion.http import polite_get

ARCTIC_API = "https://arctic-shift.photon-reddit.com/api/posts/search"


def _from_json(limit: int) -> list[dict]:
    url = f"https://www.reddit.com/r/{config.REDDIT_SUBREDDIT}/new.json?limit={limit}"
    resp = polite_get(
        url,
        headers={"Accept": "application/json"},
        timeout=config.SOURCE_REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} for new.json")
    payload = resp.json()
    posts = []
    for child in (payload.get("data", {}).get("children") or []):
        data = child.get("data") or {}
        post_id = data.get("name") or data.get("id")
        if not post_id:
            continue
        created = data.get("created_utc")
        posts.append(
            {
                "post_id": post_id,
                "title": clean_html(data.get("title"), 300),
                "author": data.get("author", ""),
                "published": (
                    datetime.fromtimestamp(float(created), tz=timezone.utc).isoformat(timespec="seconds")
                    if created
                    else ""
                ),
                "link": f"https://www.reddit.com{data.get('permalink', '')}",
                "body": clean_html(data.get("selftext"), 2500),
                "score": int(data.get("score") or 0),
            }
        )
    return posts


def _from_rss(limit: int) -> list[dict]:
    import feedparser

    url = f"https://www.reddit.com/r/{config.REDDIT_SUBREDDIT}/new.rss"
    resp = polite_get(
        url,
        headers={"Accept": "application/atom+xml, application/xml"},
        timeout=config.SOURCE_REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} for new.rss")
    parsed = feedparser.parse(resp.content)
    posts = []
    for entry in parsed.entries[:limit]:
        link = entry.get("link") or ""
        post_id = entry.get("id") or link
        if not post_id:
            continue
        posts.append(
            {
                "post_id": post_id,
                "title": clean_html(entry.get("title"), 300),
                "author": (entry.get("author") or "").lstrip("/"),
                "published": published_iso(entry),
                "link": link,
                "body": clean_html(entry.get("content", [{}])[0].get("value")
                                   if isinstance(entry.get("content"), list)
                                   else entry.get("summary"), 2500),
                "score": 0,
            }
        )
    return posts


def _from_arctic(limit: int, title: str | None = None) -> list[dict]:
    """Read the subreddit (or search titles) through the Arctic Shift mirror."""
    params = {"subreddit": config.REDDIT_SUBREDDIT, "limit": limit, "sort": "desc"}
    if title:
        params["title"] = title
    resp = polite_get(ARCTIC_API, params=params, timeout=config.SOURCE_REQUEST_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} for Arctic Shift")
    rows = (resp.json().get("data") or [])
    posts: list[dict] = []
    for row in rows:
        post_id = f"t3_{row['id']}" if row.get("id") else row.get("name")
        if not post_id:
            continue
        created = row.get("created_utc")
        posts.append(
            {
                "post_id": post_id,
                "title": clean_html(row.get("title"), 300),
                "author": row.get("author") or "",
                "published": (
                    datetime.fromtimestamp(float(created), tz=timezone.utc).isoformat(timespec="seconds")
                    if created
                    else ""
                ),
                "link": f"https://www.reddit.com{row.get('permalink') or ''}",
                "body": clean_html(row.get("selftext"), 2500),
                "score": int(row.get("score") or 0),
            }
        )
    return posts


def fetch_posts(limit: int = 50) -> list[dict]:
    """Fetch the newest subreddit posts without persisting them.

    Reddit's own endpoints throttle this network, so the mirror is the last
    resort for the freshest-posts window used by local item matching.
    """
    for attempt in (_from_json, _from_rss, _from_arctic):
        try:
            posts = attempt(limit)
            if posts:
                return posts
        except Exception:
            continue
    return []


def search_posts(query: str, limit: int = 6) -> list[dict]:
    """Title search through the Arctic Shift mirror (Reddit search is blocked).

    The mirror matches titles literally, so a multi-word item name ("Booster
    Cookie") only returns hits when the whole phrase appears. We search the
    longest significant word instead and then rank by how many name words the
    title actually contains.
    """
    tokens = [t for t in re.split(r"[^a-z0-9]+", query.lower()) if len(t) >= 4]
    if not tokens:
        return []
    term = max(tokens, key=len)
    try:
        rows = _from_arctic(limit * 3, title=term)
    except Exception:
        return []
    ranked: list[tuple[int, int, dict]] = []
    for post in rows:
        title = (post.get("title") or "").lower()
        matched = sum(1 for token in tokens if token[:5] in title)
        if matched:
            ranked.append((matched, post.get("score") or 0, post))
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [post for _, _, post in ranked[:limit]]


def collect_reddit_posts(limit: int | None = None) -> dict:
    limit = limit or config.REDDIT_POST_LIMIT
    errors: list[str] = []
    posts: list[dict] = []
    try:
        posts = _from_json(limit)
    except Exception as json_exc:
        try:
            posts = _from_rss(limit)
        except Exception as rss_exc:
            try:
                # Both Reddit endpoints throttled: read through the mirror so
                # the pipeline still gets fresh threads instead of a warning.
                posts = _from_arctic(limit)
            except Exception as arctic_exc:
                errors.append(
                    f"new.json: {json_exc}; new.rss: {rss_exc}; arctic: {arctic_exc}"
                )
                return {"items": [], "errors": errors}

    new = [p for p in posts if not database.saw_reddit_post(p["post_id"])]
    if new:
        database.save_reddit_posts(new)
    return {"items": new, "errors": errors}
