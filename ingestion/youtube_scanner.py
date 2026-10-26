"""SkyBlock YouTube scanner: creator feeds + transcripts.

YouTube's public XML feed often returns 404 from datacenter IPs, so the module
falls back to parsing the channel's /videos page. Transcripts are fetched with
youtube-transcript-api and degrade gracefully when unavailable.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import requests

import config
import database
from ingestion.http import polite_get

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
CHANNEL_ID_RE = re.compile(r'"externalId":"(UC[\w-]{22})"')
CHANNEL_FALLBACK_RE = re.compile(r'"channelId":"(UC[\w-]{22})"')
CANONICAL_RE = re.compile(r"youtube\.com/channel/(UC[\w-]{22})")
INITIAL_DATA_RE = re.compile(r"ytInitialData\s*=\s*\{", re.S)
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
RELATIVE_RE = re.compile(
    r"(\d+)\s*(second|sec|minute|min|hour|day|week|month|mo|year|y|w|d|h|s)s?\s+ago", re.I
)
_UNIT_MAP = {
    "second": "seconds", "sec": "seconds", "s": "seconds",
    "minute": "minutes", "min": "minutes",
    "hour": "hours", "h": "hours",
    "day": "days", "d": "days",
    "week": "weeks", "w": "weeks",
    "month": "months", "mo": "months",
    "year": "years", "y": "years",
}

SKYBLOCK_KEYWORDS = (
    "skyblock", "bazaar", "alpha", "patch", "update", "nerf", "buff", "ironman",
    "mining", "farming", "dungeon", "slayer", "mayor", "garden", "rift", "foraging",
    "coins", "money", "market", "economy",
)


def _get(url: str) -> str:
    resp = polite_get(
        url,
        headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
        timeout=config.SOURCE_REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} for {url}")
    return resp.text


def resolve_channel_id(handle: str) -> str | None:
    """Accept @handle, a channel URL, or a raw UC… id."""
    handle = handle.strip()
    cached = database.get_setting(f"yt_channel_id:{handle}")
    if cached:
        return cached

    if re.fullmatch(r"UC[\w-]{22}", handle):
        return handle
    match = CANONICAL_RE.search(handle)
    if match:
        return match.group(1)

    profile = handle if handle.startswith("@") else f"@{handle}"
    html = _get(f"https://www.youtube.com/{profile}")
    match = CHANNEL_ID_RE.search(html) or CHANNEL_FALLBACK_RE.search(html)
    if not match:
        return None
    channel_id = match.group(1)
    database.set_setting(f"yt_channel_id:{handle}", channel_id)
    return channel_id


def _relative_to_iso(text: str) -> str:
    match = RELATIVE_RE.search(text or "")
    if not match:
        return text or ""
    amount, unit = int(match.group(1)), match.group(2).lower()
    key = _UNIT_MAP.get(unit)
    if not key:
        return text or ""
    if key == "years":
        delta = timedelta(days=365 * amount)
    elif key == "months":
        delta = timedelta(days=30 * amount)
    else:
        delta = timedelta(**{key: amount})
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")


def _videos_from_rss(channel_id: str) -> list[dict]:
    import feedparser

    xml = _get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
    parsed = feedparser.parse(xml)
    videos = []
    for entry in parsed.entries[:5]:
        video_id = entry.get("yt_videoid") or ""
        if not VIDEO_ID_RE.match(video_id):
            match = re.search(r"v=([A-Za-z0-9_-]{11})", entry.get("link", ""))
            video_id = match.group(1) if match else ""
        if not video_id:
            continue
        videos.append(
            {
                "video_id": video_id,
                "title": entry.get("title", ""),
                "channel": entry.get("author", channel_id),
                "published": entry.get("published", ""),
                "link": f"https://www.youtube.com/watch?v={video_id}",
            }
        )
    return videos


def _walk_video_renderers(node) -> list[dict]:
    found: list[dict] = []

    def walk(value):
        if isinstance(value, dict):
            if "videoRenderer" in value and isinstance(value["videoRenderer"], dict):
                found.append(value["videoRenderer"])
            if "lockupViewModel" in value and isinstance(value["lockupViewModel"], dict):
                found.append({"__lockup__": value["lockupViewModel"]})
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return found


def _lockup_fields(lockup: dict) -> dict | None:
    """Normalise YouTube's newer `lockupViewModel` video card."""
    video_id = lockup.get("contentId", "")
    if not VIDEO_ID_RE.match(video_id):
        return None
    metadata = (lockup.get("metadata") or {}).get("lockupMetadataViewModel") or {}
    title = (metadata.get("title") or {}).get("content") or ""

    published = ""
    rows = (
        ((metadata.get("metadata") or {}).get("contentMetadataViewModel") or {}).get(
            "metadataRows"
        )
        or []
    )
    for row in rows:
        for part in row.get("metadataParts") or []:
            text = ((part.get("text") or {}).get("content") or "").strip()
            if RELATIVE_RE.search(text):
                published = _relative_to_iso(text)
    return {
        "video_id": video_id,
        "title": title,
        "published": published,
        "link": f"https://www.youtube.com/watch?v={video_id}",
    }


def _videos_from_page(handle: str) -> list[dict]:
    profile = handle if handle.startswith("@") else f"@{handle}"
    html = _get(f"https://www.youtube.com/{profile}/videos")
    match = INITIAL_DATA_RE.search(html)
    if not match:
        raise RuntimeError("ytInitialData not found on channel page")
    try:
        data, _ = json.JSONDecoder().raw_decode(html[match.end() - 1 :])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ytInitialData parse failed: {exc}") from exc
    videos = []
    for renderer in _walk_video_renderers(data)[:6]:
        if "__lockup__" in renderer:
            parsed = _lockup_fields(renderer["__lockup__"])
            if parsed:
                parsed["channel"] = profile
                videos.append(parsed)
            continue
        video_id = renderer.get("videoId", "")
        if not VIDEO_ID_RE.match(video_id):
            continue
        title = renderer.get("title", {})
        title_text = (
            (title.get("runs") or [{}])[0].get("text")
            or title.get("simpleText")
            or ""
        )
        videos.append(
            {
                "video_id": video_id,
                "title": title_text,
                "channel": profile,
                "published": _relative_to_iso(
                    (renderer.get("publishedTimeText") or {}).get("simpleText", "")
                ),
                "link": f"https://www.youtube.com/watch?v={video_id}",
            }
        )
    if not videos:
        raise RuntimeError("no videoRenderer entries on channel page")
    return videos


def search_videos(query: str, limit: int = 4) -> list[dict]:
    """Search YouTube's results page for a query and normalise the videos.

    Used by the active per-ticker search. The description snippet is stored in
    `transcript` so downstream evidence scoring sees more than the title.
    """
    from urllib.parse import quote_plus

    html = _get(f"https://www.youtube.com/results?search_query={quote_plus(query)}")
    match = INITIAL_DATA_RE.search(html)
    if not match:
        raise RuntimeError("ytInitialData not found on search page")
    try:
        data, _ = json.JSONDecoder().raw_decode(html[match.end() - 1 :])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ytInitialData parse failed: {exc}") from exc

    videos: list[dict] = []
    seen: set[str] = set()
    for renderer in _walk_video_renderers(data):
        if len(videos) >= limit:
            break
        if "__lockup__" in renderer:
            parsed = _lockup_fields(renderer["__lockup__"])
            if parsed and parsed["video_id"] not in seen:
                seen.add(parsed["video_id"])
                parsed["channel"] = ""
                videos.append(parsed)
            continue
        video_id = renderer.get("videoId", "")
        if not VIDEO_ID_RE.match(video_id) or video_id in seen:
            continue
        title = renderer.get("title", {})
        title_text = (
            (title.get("runs") or [{}])[0].get("text") or title.get("simpleText") or ""
        )
        snippet = ""
        for part in renderer.get("detailedMetadataSnippets") or []:
            runs = ((part.get("snippetText") or {}).get("runs") or [])
            snippet += "".join(r.get("text", "") for r in runs)
        owner_runs = (renderer.get("ownerText") or {}).get("runs") or []
        seen.add(video_id)
        videos.append(
            {
                "video_id": video_id,
                "title": title_text,
                "channel": owner_runs[0].get("text", "") if owner_runs else "",
                "published": _relative_to_iso(
                    (renderer.get("publishedTimeText") or {}).get("simpleText", "")
                ),
                "link": f"https://www.youtube.com/watch?v={video_id}",
                "transcript": snippet[:800],
            }
        )
    return videos


def fetch_transcript(video_id: str) -> str:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        if hasattr(YouTubeTranscriptApi, "fetch"):
            fetched = YouTubeTranscriptApi().fetch(video_id, languages=["en"])
            parts = [getattr(s, "text", "") for s in fetched]
        else:  # older library versions
            data = YouTubeTranscriptApi.get_transcript(video_id, languages=["en"])
            parts = [d.get("text", "") for d in data]
        return " ".join(p for p in parts if p)[:8000]
    except Exception:
        return ""


def collect_youtube_videos(per_channel: int = 2) -> dict:
    items: list[dict] = []
    errors: list[str] = []
    transcripts_left = config.YOUTUBE_MAX_TRANSCRIPTS

    for handle in config.YOUTUBE_CREATORS:
        try:
            channel_id = resolve_channel_id(handle)
        except Exception as exc:
            errors.append(f"{handle}: channel lookup failed: {exc}")
            channel_id = None

        videos: list[dict] = []
        if channel_id:
            try:
                videos = _videos_from_rss(channel_id)
            except Exception:
                videos = []  # public XML feed is frequently 404; use the page
        if not videos:
            try:
                videos = _videos_from_page(handle)
            except Exception as exc:
                errors.append(f"{handle}: {exc}")
                continue

        if not videos:
            errors.append(f"{handle}: feed returned no videos")
            continue
        for video in videos[:per_channel]:
            title = video.get("title", "")
            if not any(k in title.lower() for k in SKYBLOCK_KEYWORDS):
                continue
            if database.saw_video(video["video_id"]):
                continue
            if transcripts_left > 0:
                video["transcript"] = fetch_transcript(video["video_id"])
                transcripts_left -= 1
            items.append(video)

    if items:
        database.save_videos(items)
    return {"items": items, "errors": errors}
