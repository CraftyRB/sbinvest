"""Confidence-weighted cross-validation matrix.

Score = W_github*S_github + W_forum*S_forum + W_youtube*S_youtube
      + W_reddit*S_reddit + W_bazaar*S_bazaar

Single-source signals are capped so a lone YouTube/Reddit pump cannot become a
high-conviction BUY. Multi-source confluence (>=2 independent streams) unlocks
the high-conviction tier.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import config
import database
from analytics.portfolio import sector_for

# Sector-wide wording that shows up in patch notes, videos and posts without ever
# naming a specific Bazaar id. A hit here is partial evidence, never a full match.
THEME_KEYWORDS = {
    "FARMING": ("farming fortune", "garden update", "compost", "greenhouse", "crop growth"),
    "MINING": ("glacite", "dwarven mines", "crystal hollows", "gemstone", "mithril", "mining fortune", "powder"),
    "COMBAT": ("slayer boss", "slayer", "dungeon", "bestiary", "kuudra", "crimson isle", "dragon"),
    "FISHING": ("trophy fishing", "trophy frog", "sea creature", "lotus atoll"),
    "FORAGING": ("foraging", "sapling", "lumber", "galatea"),
    "ALCHEMY": ("alchemy", "potion", "brewing", "enchanting", "enchantment"),
}
THEMATIC_CREDIT = {
    "github": 0.55,
    "forum": 0.50,
    "youtube": 0.40,
    "reddit": 0.35,
    "wiki": 0.35,
    "web": 0.30,
}
STREAMS = ("github", "forum", "youtube", "reddit", "wiki", "web")
PATCH_NOTE_BONUS = 0.10
AI_SEMANTIC_DISCOUNT = 0.7

# Stale chatter should not move conviction: evidence older than 21 days is
# dimmed by up to 40% (floor 0.60) in its stream score.
FRESH_DAYS = 3
STALE_DAYS = 21
DECAY_FLOOR = 0.60


def _doc_days_ago(doc: dict | None) -> float | None:
    """Days since the document's own timestamp; None when unparseable."""
    if not doc:
        return None
    stamp = doc.get("ts") or doc.get("published") or doc.get("created_at")
    iso = database._normalize_ts(stamp)
    if not iso:
        return None
    published = datetime.fromisoformat(iso)
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - published).total_seconds() / 86400.0


def _age_decay(docs: list[dict] | None) -> float:
    """1.0 for fresh evidence, ~DECAY_FLOOR for anything older than STALE_DAYS.

    Uses the freshest document's age so one recent post keeps a stream hot,
    while a stream whose only evidence is old chatter gets dimmed.
    """
    ages = [d for d in (_doc_days_ago(doc) for doc in (docs or [])) if d is not None]
    if not ages:
        return 1.0
    days = min(ages)
    if days <= FRESH_DAYS:
        return 1.0
    if days >= STALE_DAYS:
        return DECAY_FLOOR
    span = STALE_DAYS - FRESH_DAYS
    return 1.0 - (1.0 - DECAY_FLOOR) * (days - FRESH_DAYS) / span


def _needles(item_id: str) -> list[str]:
    phrase = item_id.replace("_", " ").lower()
    return [phrase, item_id.lower()] if len(phrase) >= 7 else [item_id.lower()]


def _text_of_post(post: dict) -> str:
    return f"{post.get('title') or ''} {post.get('summary') or ''}".lower()


def _text_of_video(video: dict) -> str:
    return f"{video.get('title') or ''} {video.get('transcript') or ''}".lower()


def _text_of_reddit(post: dict) -> str:
    return f"{post.get('title') or ''} {post.get('body') or ''}".lower()


def _text_of_wiki(change: dict) -> str:
    return f"{change.get('title') or ''} {change.get('comment') or ''}".lower()


def _text_of_web(result: dict) -> str:
    return f"{result.get('title') or ''} {result.get('snippet') or ''}".lower()


def _matches(item_id: str, sources: dict[str, list[dict]]) -> dict[str, list[dict]]:
    needles = _needles(item_id)
    matched: dict[str, list[dict]] = {stream: [] for stream in STREAMS}

    for leak in sources.get("github") or []:
        keys = [str(i).lower() for i in (leak.get("items") or [])]
        text = f"{leak.get('title') or ''} {leak.get('diff') or ''}".lower()
        if item_id.lower() in keys or any(n in text for n in needles):
            matched["github"].append(leak)

    for post in sources.get("forum") or []:
        if any(n in _text_of_post(post) for n in needles):
            matched["forum"].append(post)

    for video in sources.get("youtube") or []:
        if any(n in _text_of_video(video) for n in needles):
            matched["youtube"].append(video)

    for post in sources.get("reddit") or []:
        if any(n in _text_of_reddit(post) for n in needles):
            matched["reddit"].append(post)

    for change in sources.get("wiki") or []:
        if any(n in _text_of_wiki(change) for n in needles):
            matched["wiki"].append(change)

    for result in sources.get("web") or []:
        if any(n in _text_of_web(result) for n in needles):
            matched["web"].append(result)

    return matched


def _thematic_matches(
    item_id: str, sources: dict[str, list[dict]], exact: dict[str, list[dict]]
) -> dict[str, list[dict]]:
    """Sector-level evidence used only when no document names the item outright."""
    sector = sector_for(item_id)
    words = THEME_KEYWORDS.get(sector)
    result: dict[str, list[dict]] = {"github": [], "forum": [], "youtube": [], "reddit": []}
    if not words:
        return result

    text_of = {"forum": _text_of_post, "youtube": _text_of_video, "reddit": _text_of_reddit}
    for stream in ("github", "forum", "youtube", "reddit"):
        if exact.get(stream):
            continue  # an exact hit already scores this stream
        for doc in sources.get(stream) or []:
            if stream == "github":
                text = f"{doc.get('title') or ''} {doc.get('diff') or ''}".lower()
            else:
                text = text_of[stream](doc)
            phrase = next((word for word in words if word in text), None)
            if not phrase:
                continue
            title = doc.get("title") or doc.get("sha") or ""
            result[stream].append(
                {
                    "phrase": phrase,
                    "title": title[:70],
                    "sector": sector,
                    "feed": (doc.get("feed") or "").lower(),
                }
            )
            break
    return result


def _github_score(item_id: str, matched: list[dict]) -> float:
    if not matched:
        return 0.0
    if any(item_id.lower() in [str(i).lower() for i in (leak.get("items") or [])] for leak in matched):
        return 1.0
    return 0.7


def _forum_score(matched: list[dict]) -> float:
    if not matched:
        return 0.0
    best = 0.5
    for post in matched:
        feed = (post.get("feed") or "").lower()
        if "alpha" in feed or "patch notes" in feed:
            best = 1.0
    return best


def _youtube_score(matched: list[dict]) -> float:
    if not matched:
        return 0.0
    score = 1.0 if len(matched) >= 2 else 0.55
    if any((video.get("transcript") or "") for video in matched):
        score = min(1.0, score + 0.15)
    return score


def _reddit_score(matched: list[dict]) -> float:
    if not matched:
        return 0.0
    top_score = max(int(post.get("score") or 0) for post in matched)
    engagement = 1.0 if top_score >= 100 else 0.7 if top_score >= 20 else 0.4
    base = min(1.0, 0.30 + 0.15 * len(matched) + 0.35 * engagement)
    return base


def _wiki_score(matched: list[dict]) -> float:
    """Wiki pages are reference facts, not sentiment: solid but not decisive."""
    if not matched:
        return 0.0
    return min(0.9, 0.55 + 0.1 * len(matched))


def _web_score(matched: list[dict]) -> float:
    if not matched:
        return 0.0
    return min(0.8, 0.45 + 0.12 * len(matched))


def _bazaar_score(candidate: dict, buy_threshold: float) -> float:
    if candidate.get("has_history"):
        # Young histories (2-3 distinct days) are still thin: dampen the
        # valuation component so a short-lived MA cannot drive conviction.
        reliability = min(1.0, 0.5 + 0.1 * float(candidate.get("horizon_days", 2)))
        valuation = max(0.0, min(1.0, (buy_threshold - candidate["risk_ratio"]) / 0.10)) * reliability
    elif candidate.get("has_intraday"):
        # Same-day dips are real but mean-revert fast, so they count for less.
        # The ratio gap and the statistical dip each supply half the score.
        gap = config.INTRADAY_BUY_THRESHOLD - candidate["risk_ratio"]
        ratio_part = max(0.0, min(1.0, gap / 0.05))
        z_part = max(0.0, min(1.0, -float(candidate.get("dip_z") or 0.0) / 2.0))
        valuation = (0.5 * ratio_part + 0.5 * z_part) * 0.7
    else:
        valuation = 0.25
    liquidity = min(1.0, math.log10(max(candidate["weekly_volume_coins"], 1.0) / 100_000) / 2)
    spread_penalty = min(0.25, abs(candidate.get("spread_pct", 0.0)) / 50)
    return max(0.0, min(1.0, 0.65 * valuation + 0.35 * liquidity - spread_penalty))


def _labels(
    item_id: str,
    matched: dict[str, list[dict]],
    sources: dict,
    thematic: dict[str, list[dict]] | None = None,
    ai_link: dict[str, dict] | None = None,
) -> list[str]:
    labels: list[str] = []
    for leak in matched["github"]:
        labels.append(f"GitHub leak: {(leak.get('title') or 'commit')[:80]}")
    for post in matched["forum"]:
        labels.append(f"Forum: {(post.get('title') or 'post')[:80]}")
    for video in matched["youtube"]:
        labels.append(f"YouTube: {(video.get('channel') or '')[:24]} - {(video.get('title') or '')[:60]}")
    for post in matched["reddit"]:
        labels.append(f"Reddit: {(post.get('title') or '')[:80]}")
    for change in matched["wiki"]:
        labels.append(f"Wiki: {(change.get('title') or '')[:70]}")
    for result in matched["web"]:
        labels.append(f"Web: {(result.get('title') or '')[:70]}")

    for stream in ("github", "forum", "youtube", "reddit"):
        for hit in (thematic or {}).get(stream) or []:
            labels.append(
                f"Thematic {hit['sector']}: '{hit['phrase']}' in {hit['title']}"
            )

    for stream, hit in (ai_link or {}).items():
        relevance = float(hit.get("relevance") or 0)
        if relevance <= 0:
            continue
        why = hit.get("why") or ""
        labels.append(
            f"AI {stream} (rel {relevance:.2f}): {hit.get('title') or 'evidence'}"
            + (f" — {why}" if why else "")
        )

    for row in sources.get("auction") or []:
        if row.get("tag", "").upper() == item_id.upper():
            labels.append(f"AH LBIN: {row['tag']} median {row.get('median')}")
    return labels


def score_candidates(
    candidates: list[dict],
    sources: dict[str, Any],
    threshold: dict[str, Any],
    ai_links: dict[str, dict[str, dict]] | None = None,
) -> list[dict]:
    """Enrich candidates with confluence score, tier and evidence attribution."""
    buy_threshold = float(threshold.get("buy_threshold", config.BASE_BUY_THRESHOLD))
    weights = config.CONFLUENCE_WEIGHTS
    ai_links = ai_links or {}
    enriched: list[dict] = []

    for candidate in candidates:
        item_id = candidate["item_id"]
        matched = _matches(item_id, sources)
        thematic = _thematic_matches(item_id, sources, matched)
        stream_scores = {
            "github": _github_score(item_id, matched["github"]),
            "forum": _forum_score(matched["forum"]),
            "youtube": _youtube_score(matched["youtube"]),
            "reddit": _reddit_score(matched["reddit"]),
            "wiki": _wiki_score(matched["wiki"]),
            "web": _web_score(matched["web"]),
            "bazaar": _bazaar_score(candidate, buy_threshold),
        }
        for stream in STREAMS:
            if stream_scores[stream] > 0 or not thematic.get(stream):
                continue
            hit = thematic[stream][0]
            credit = THEMATIC_CREDIT[stream]
            if stream == "forum" and ("patch" in hit["feed"] or "alpha" in hit["feed"]):
                credit = min(1.0, credit + PATCH_NOTE_BONUS)
            credit *= _age_decay(thematic[stream])
            stream_scores[stream] = credit

        ai_link = ai_links.get(item_id) or {}
        for stream in STREAMS:
            relevance = float((ai_link.get(stream) or {}).get("relevance") or 0)
            if relevance and stream_scores[stream] == 0:
                stream_scores[stream] = round(relevance * AI_SEMANTIC_DISCOUNT, 3)

        for stream in STREAMS:
            if stream_scores[stream] > 0:
                stream_scores[stream] = stream_scores[stream] * _age_decay(matched[stream])
        score = sum(weights[stream] * value for stream, value in stream_scores.items())

        external = sum(1 for stream in STREAMS if stream_scores[stream] > 0)
        if external == 0:
            score = min(score, config.NO_EXTERNAL_SOURCE_CAP)
        elif external == 1:
            score = min(score, config.SINGLE_SOURCE_CAP)
        score = round(max(0.0, min(1.0, score)), 3)

        if score >= config.HIGH_CONVICTION_SCORE and external >= 2:
            tier = "HIGH"
        elif score >= 0.35:
            tier = "MEDIUM"
        else:
            tier = "LOW"

        enriched.append(
            {
                **candidate,
                "confluence_score": score,
                "risk_tier": tier,
                "evidence_count": external,
                "stream_scores": {k: round(v, 3) for k, v in stream_scores.items()},
                "evidence_sources": _labels(item_id, matched, sources, thematic, ai_link),
            }
        )

    enriched.sort(
        key=lambda c: (c["confluence_score"], c["composite_score"]),
        reverse=True,
    )
    return enriched
