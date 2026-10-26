"""MediaWiki recent-changes watcher for the Hypixel SkyBlock Wiki.

The official wiki.hypixel.net API was shut down (it now 302s to a forum post),
so the module probes each configured endpoint and falls back to the community
Fandom MediaWiki API, which is still live.
"""

from __future__ import annotations

import config
import database
from ingestion.http import polite_get


class WikiError(RuntimeError):
    pass


def _query(api_url: str, limit: int) -> list[dict]:
    params = {
        "action": "query",
        "list": "recentchanges",
        "rcprop": "title|timestamp|user|comment|ids",
        "rclimit": limit,
        "rcnamespace": "0|114",
        "format": "json",
    }
    resp = polite_get(
        api_url,
        params=params,
        timeout=config.SOURCE_REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        raise WikiError(f"HTTP {resp.status_code} for {api_url}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise WikiError(f"Non-JSON response from {api_url}") from exc

    changes = []
    for row in (payload.get("query") or {}).get("recentchanges") or []:
        revid = row.get("revid")
        if not revid:
            continue
        changes.append(
            {
                "revid": str(revid),
                "title": row.get("title"),
                "user": row.get("user"),
                "comment": row.get("comment") or "",
                "ts": row.get("timestamp"),
                "wiki": api_url,
            }
        )
    return changes


def collect_wiki_changes(limit: int = 15) -> dict:
    items: list[dict] = []
    errors: list[str] = []
    for api_url in config.WIKI_API_URLS:
        try:
            items = _query(api_url, limit)
        except Exception as exc:
            errors.append(f"{api_url}: {exc}")
            continue
        if items:
            break

    new = [c for c in items if not database.saw_wiki_rev(c["revid"])]
    if new:
        database.save_wiki_changes(new)
    return {"items": new, "errors": errors}
