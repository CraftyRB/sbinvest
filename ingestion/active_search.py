"""Targeted per-ticker evidence search.

The passive scanners poll a fixed firehose, so after ranking there is often
almost nothing that mentions a specific Bazaar item. This module searches each
top candidate's name directly on YouTube, the Fandom wiki and the open web
(DuckDuckGo), and matches the freshest Reddit posts against the item name
locally. Hits are persisted in the cache tables and handed back to the pipeline.
All requests share the per-host throttle in `ingestion/http.py` so bursts do not
trip rate limits.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import config
import database
from ingestion.feed_utils import clean_html
from ingestion.http import polite_get
from ingestion.reddit_scanner import fetch_posts, search_posts
from ingestion.web_search import WebSearchBlocked, search_web
from ingestion.youtube_scanner import search_videos

FANDOM_API = "https://hypixel-skyblock.fandom.com/api.php"
TIMEOUT = min(10, int(config.SOURCE_REQUEST_TIMEOUT))
_TRAILING_NUM = re.compile(r"\s+\d+$")


def _display(name: str, item_id: str) -> str:
    given = (name or "").strip()
    if given:
        return given
    text = item_id.replace("_", " ").strip()
    return _TRAILING_NUM.sub("", text).strip() or text


def _match_reddit(posts: list[dict], name: str, limit: int) -> list[dict]:
    """Keep the newest subreddit posts that actually mention this item.

    Reddit's search endpoints reject this network, so instead of querying with
    a search term we filter the freshest posts we can fetch. Unmatched posts are
    not returned: the evidence gate would only drop them again.
    """
    needles = {name.lower(), name.lower().replace(" ", "_")}
    matched: list[dict] = []
    for post in posts:
        text = f"{post.get('title') or ''} {post.get('body') or ''}".lower()
        if any(needle in text for needle in needles):
            matched.append(post)
            if len(matched) >= limit:
                break
    return matched


def _clean_wikitext(raw: str) -> str:
    """Flatten MediaWiki markup into readable prose (templates, links, tags)."""
    text = raw or ""
    for _ in range(3):  # nested templates need repeated passes
        text = re.sub(r"\{\{[^{}]*\}\}", " ", text)
    text = re.sub(r"\[\[File:[^\]]*\]\]", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[https?://\S+\s+([^\]]+)\]", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"'{2,}", "", text)
    text = re.sub(r"^=+\s*|\s*=+$", " ", text, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", text).strip()


def _wiki_page(title: str, limit: int = 600) -> dict | None:
    """Intro of the exact page title.

    TextExtracts is disabled on this wiki, so the intro comes from the raw
    wikitext of section 0, cleaned of templates and markup.
    """
    resp = polite_get(
        FANDOM_API,
        params={
            "action": "parse",
            "page": title,
            "prop": "wikitext",
            "section": "0",
            "redirects": "1",
            "format": "json",
        },
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    page = payload.get("parse") or {}
    text = _clean_wikitext((page.get("wikitext") or {}).get("*") or "")
    if len(text) < 40:
        return None
    return {
        "revid": f"page:{page.get('pageid')}",
        "title": page.get("title") or title,
        "user": "",
        "comment": clean_html(text, limit),
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "wiki": FANDOM_API,
    }


def search_wiki(query: str, limit: int = 4) -> list[dict]:
    resp = polite_get(
        FANDOM_API,
        params={
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": limit,
            "format": "json",
        },
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} for {FANDOM_API}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"Non-JSON response from {FANDOM_API}") from exc

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changes: list[dict] = []
    # The exact item page is the best evidence, and search often misses it
    # (templated pages rank poorly), so fetch it directly first.
    direct = _wiki_page(query)
    if direct:
        changes.append(direct)
    for row in (payload.get("query") or {}).get("search") or []:
        page_id = row.get("pageid")
        if not page_id:
            continue
        title = row.get("title") or ""
        if any(c["title"] == title for c in changes):
            continue
        changes.append(
            {
                "revid": f"search:{page_id}",
                "title": title,
                "user": "",
                "comment": clean_html(row.get("snippet"), 600),
                "ts": row.get("timestamp") or now,
                "wiki": FANDOM_API,
            }
        )
    return changes[:limit]


def _ddg(query: str, fallback: str, limit: int) -> tuple[list[dict], str | None]:
    """DuckDuckGo search with a relaxed second query when the first returns nothing."""
    docs: list[dict] = []
    error: str | None = None
    for attempt_query in (query, fallback):
        try:
            docs.extend(search_web(attempt_query, limit))
        except WebSearchBlocked as exc:
            # A block is network-wide; a second query would just repeat it.
            return docs, str(exc)
        except Exception as exc:
            error = str(exc)
        if len(docs) >= limit:
            break
    return docs, (None if docs else error)


def _run(kind: str, name: str, limit: int, query: str | None = None) -> tuple[list[dict], str | None]:
    if kind == "reddit":
        # Title search via the Arctic Shift mirror: reddit.com's own search
        # endpoints reject this network.
        return search_posts(name, limit), None
    if kind == "youtube":
        return search_videos(f"{name} skyblock bazaar", limit), None
    if kind == "wiki":
        return search_wiki(name, limit), None
    if kind == "web":
        if not name:
            # Planner global query: sector-wide update news, no item attached.
            return _ddg(query or "", query or "", limit)
        return _ddg(query or f'"{name}" Hypixel SkyBlock', f"{name} skyblock bazaar", limit)
    raise ValueError(f"unknown stream {kind}")


def active_search(
    candidates: list[dict],
    limit: int = 6,
    per_source: int = 4,
    query_map: dict[str, list[str]] | None = None,
    global_queries: list[str] | None = None,
) -> dict:
    """Search the top candidates' names directly and cache the hits.

    When the research planner supplied per-ticker queries, those replace the
    generic item-name query on the open web, so the search and the evidence
    gate are looking for the same thing. `global_queries` add sector-wide
    update news that is not tied to a single item.

    Returns {"items": {stream: docs}, "errors": [...], "queries": n}.
    """
    picks = candidates[:limit]
    items: dict[str, list[dict]] = {"reddit": [], "youtube": [], "wiki": [], "web": []}
    errors: list[str] = []
    if not picks:
        return {"items": items, "errors": errors, "queries": 0}

    named = [
        (candidate, _display(candidate.get("item_name") or "", candidate["item_id"]))
        for candidate in picks
    ]

    # Reddit: one feed fetch per run (the search endpoints are blocked), then a
    # local item-name match so we still surface threads that mention the item.
    try:
        posts = fetch_posts(60)
    except Exception as exc:
        posts = []
        errors.append(f"reddit feed: {exc}")
    seen_posts: set[str] = set()
    for _, name in named:
        for post in _match_reddit(posts, name, per_source):
            post_id = post.get("post_id")
            if post_id and post_id not in seen_posts:
                seen_posts.add(post_id)
                items["reddit"].append(post)

    jobs: list[tuple[str, str, str | None]] = []
    for candidate, name in named:
        ticker = str(candidate["item_id"]).upper()
        planned = [
            q for q in (query_map or {}).get(ticker) or [] if str(q).strip()
        ][: max(1, config.PLANNER_QUERIES_PER_ITEM)]
        jobs.append(("reddit", name, None))
        jobs.append(("youtube", name, None))
        jobs.append(("wiki", name, None))
        if planned:
            for query in planned:
                jobs.append(("web", name, query))
        else:
            jobs.append(("web", name, None))
    for query in (global_queries or [])[:3]:
        if str(query).strip():
            jobs.append(("web", "", str(query)))
    if not jobs:
        return {"items": items, "errors": errors, "queries": 0}

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(_run, kind, name, per_source, query): (kind, name)
            for kind, name, query in jobs
        }
        for future, (kind, name) in futures.items():
            try:
                docs, error = future.result()
            except Exception as exc:
                errors.append(f"{kind} search '{name}': {exc}")
                continue
            if error:
                errors.append(f"{kind} search '{name}': {error}")
            docs = docs or []
            seen = {
                d.get("post_id") or d.get("video_id") or d.get("revid") or d.get("result_id")
                for d in items[kind]
            }
            for doc in docs[:per_source]:
                key = (
                    doc.get("post_id") or doc.get("video_id")
                    or doc.get("revid") or doc.get("result_id")
                )
                if key and key not in seen:
                    seen.add(key)
                    items[kind].append(doc)

    if items["reddit"]:
        database.save_reddit_posts(items["reddit"])
    if items["youtube"]:
        database.save_videos(items["youtube"])
    if items["wiki"]:
        database.save_wiki_changes(items["wiki"])
    if items["web"]:
        database.save_web_results(items["web"])
    return {"items": items, "errors": errors, "queries": len(jobs)}
