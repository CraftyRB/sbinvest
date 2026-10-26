"""End-to-end scan: ingest 8 feeds -> confluence scoring -> adversarial AI debate
-> persist -> alert.

This is what the Streamlit "Run scan" button, the daemon and the CLI invoke.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv

load_dotenv("env.txt", override=False)

import config
import database
from analytics import (
    adaptive_threshold,
    grade_predictions,
    rank_candidates,
    score_candidates,
    sector_for,
    value_holdings,
)
from ingestion import (
    BazaarError,
    LINKABLE_SECTORS,
    active_search,
    collect_forum_posts,
    collect_new_leaks,
    collect_reddit_posts,
    collect_update_calendar,
    collect_youtube_videos,
    fetch_bazaar,
    normalize_products,
)
from ingestion.calendar_mayor import collect_mayor_context
from ingestion.coflnet_ah import collect_ah_intel
from ingestion.github_watcher import GitHubError
from ingestion.wiki_watcher import collect_wiki_changes
from intelligence import analyze, filter_evidence, plan_research


def _recent_open_items(hours: int = 12) -> set[tuple[str, str]]:
    """Recently stored (item_id, term) pairs so the same call is not re-stored."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    return {
        (s["item_id"], (s.get("term") or "SHORT").upper())
        for s in database.list_signals(status="open", limit=500)
        if s["created_at"] >= cutoff
    }


def _resolve_open_signals(price_map: dict[str, float], notify: bool = True) -> list[dict]:
    """Auto-close open signals against the freshest snapshot.

    BUY calls close on target hit, stop hit, or once their horizon lapses;
    AVOID/SELL calls close when price reclaims entry (thesis invalidated) or
    the horizon lapses. The win/loss stamping happens in database.resolve_signal,
    which feeds the win-rate stats that adapt the buy threshold.
    """
    resolved: list[dict] = []
    # HOLD calls are watch notes, not positions: retire them after a day so the
    # open book stays focused on actionable trades.
    database.expire_holds(hours=24)
    if not price_map:
        return resolved
    now = datetime.now(timezone.utc)
    for sig in database.list_signals(status="open", limit=200):
        sig_type = (sig.get("signal") or "").upper()
        # Stale HOLDs were expired above; any still open is under a day old and
        # has no directional thesis to score. Only BUY/SELL feed the win-rate loop.
        if sig_type not in {"BUY", "SELL"}:
            continue
        mid = price_map.get(sig["item_id"])
        entry = float(sig.get("entry_price") or 0)
        if not mid or mid <= 0 or entry <= 0:
            continue
        target = sig.get("target_price")
        stop = sig.get("stop_price")
        due = False
        reason = "horizon"
        if sig_type == "BUY":
            if stop and mid <= float(stop):
                due, reason = True, "stop"  # stopped out -> loss
            elif target and mid >= float(target):
                due, reason = True, "target"  # target reached -> win
        else:  # SELL / AVOID: thesis invalid if price reclaims entry
            if mid >= entry:
                due, reason = True, "thesis_invalidated"
        if not due:
            try:
                opened = datetime.fromisoformat(sig["created_at"])
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
                age_days = (now - opened).total_seconds() / 86400.0
                due = age_days >= float(sig.get("horizon_days") or 14)
            except (TypeError, ValueError):
                pass
        if due:
            try:
                outcome = database.resolve_signal(sig["id"], mid)
            except ValueError:
                continue
            resolved.append(outcome)
            # "Ready to sell" push: the webhook fires on target/stop/horizon
            # regardless of the new-scan alert toggle.
            if notify:
                import alerts

                alerts.push_exit(sig, outcome, mid, reason)
    return resolved


def _portfolio_exit_alerts() -> dict[str, Any]:
    """Push cash-out alerts for holdings the user actually owns.

    This is independent of the signal stream: it reads every portfolio, marks
    each holding to the latest bid and fires on take-profit / stop tiers to the
    dedicated portfolio webhook.
    """
    import alerts

    prices = database.latest_prices()
    sent = 0
    triggered: list[str] = []
    for book in database.list_portfolios():
        holdings = database.list_holdings(book["id"])
        if not holdings:
            continue
        rows = value_holdings(holdings, prices)
        result = alerts.push_portfolio_exits(rows, book["name"])
        sent += int(result.get("sent") or 0)
        triggered.extend(result.get("triggered") or [])
        # User-placed partial exits ("sell 6 units at price X") fire at the
        # chosen price, not at a percentage tier.
        orders = database.list_exit_orders(book["id"], status="pending")
        planned = alerts.push_exit_order_hits(orders, prices, book["name"])
        sent += int(planned.get("sent") or 0)
        triggered.extend(planned.get("hit") or [])
    return {"sent": sent, "triggered": triggered}


def refresh_prices() -> int:
    """Snapshot bazaar prices without invoking the AI (portfolio tab)."""
    database.init_db()
    payload = fetch_bazaar()
    return database.record_snapshots(normalize_products(payload))


def _collect(label: str, fn, warnings: list[str]) -> Any:
    """Run one feed, converting failures into a warning instead of a crash."""
    try:
        return fn()
    except Exception as exc:
        warnings.append(f"{label} unavailable: {exc}")
        return None


def _collect_feeds(commit_limit: int, persist: bool, warnings: list[str]) -> dict[str, dict]:
    """Poll every passive feed once, turning failures into warnings."""
    github = _collect(
        "GitHub",
        lambda: {"items": collect_new_leaks(commit_limit=commit_limit), "errors": []},
        warnings,
    ) or {"items": [], "errors": []}
    warnings.extend(github.get("errors") or [])

    forum = _collect("Forum RSS", lambda: collect_forum_posts(), warnings) or {
        "items": [],
        "errors": [],
    }
    warnings.extend(forum.get("errors") or [])

    youtube = _collect("YouTube", lambda: collect_youtube_videos(), warnings) or {
        "items": [],
        "errors": [],
    }
    warnings.extend(youtube.get("errors") or [])

    reddit = _collect("Reddit", lambda: collect_reddit_posts(), warnings) or {
        "items": [],
        "errors": [],
    }
    warnings.extend(reddit.get("errors") or [])

    wiki = _collect("Wiki", lambda: collect_wiki_changes(), warnings) or {
        "items": [],
        "errors": [],
    }
    warnings.extend(wiki.get("errors") or [])

    mayor = _collect("Mayor calendar", lambda: collect_mayor_context(), warnings) or {
        "snapshot": None,
        "errors": [],
    }
    warnings.extend(mayor.get("errors") or [])

    # Auction House LBIN: prioritise item tags appearing in leaks.
    leak_tags = [
        tag
        for leak in (database.recent_leaks(limit=3) if persist else [])
        for tag in (leak.get("items") or [])
    ]
    auction = _collect("Coflnet AH", lambda: collect_ah_intel(leak_tags, limit=6), warnings) or {
        "items": [],
        "errors": [],
    }
    warnings.extend(auction.get("errors") or [])

    return {
        "github": github,
        "forum": forum,
        "youtube": youtube,
        "reddit": reddit,
        "wiki": wiki,
        "mayor": mayor,
        "auction": auction,
    }


def _cached_sources(
    feeds: dict[str, dict], persist: bool, lookback_days: int | None = None
) -> dict[str, Any]:
    """Assemble the evidence bundle, preferring the persisted cache."""
    lookback = lookback_days or config.SOURCE_LOOKBACK_DAYS
    return {
        "github": database.recent_leaks(limit=4, days=lookback)
        if persist
        else (feeds["github"].get("items") or []),
        "forum": database.recent_forum_posts(limit=20, days=lookback)
        if persist
        else (feeds["forum"].get("items") or []),
        "youtube": database.recent_videos(limit=6, days=lookback)
        if persist
        else (feeds["youtube"].get("items") or []),
        "reddit": database.recent_reddit_posts(limit=25, days=lookback)
        if persist
        else (feeds["reddit"].get("items") or []),
        "auction": database.recent_auction_stats(limit=8, days=lookback)
        if persist
        else (feeds["auction"].get("items") or []),
        "wiki": database.recent_wiki_changes(limit=10, days=lookback)
        if persist
        else (feeds["wiki"].get("items") or []),
        "web": database.recent_web_results(limit=24, days=lookback) if persist else [],
        "macro": (
            feeds["mayor"].get("snapshot") or (database.current_mayor() if persist else None) or {}
        ),
    }


def _merge_active_search(
    candidates: list[dict],
    sources: dict[str, Any],
    persist: bool,
    warnings: list[str],
    query_map: dict[str, list[str]] | None = None,
    global_queries: list[str] | None = None,
) -> dict:
    """Run the targeted per-ticker search and merge fresh hits into `sources`."""
    if not (config.ACTIVE_SEARCH_ENABLED and candidates):
        return {}
    if persist:
        found = _collect(
            "Active search",
            lambda: active_search(
                candidates,
                limit=config.ACTIVE_SEARCH_ITEMS,
                per_source=config.ACTIVE_SEARCH_RESULTS,
                query_map=query_map,
                global_queries=global_queries,
            ),
            warnings,
        ) or {}
    else:
        found = {}
    warnings.extend(found.get("errors") or [])
    for stream, key, stamp in (
        ("reddit", "post_id", "published"),
        ("youtube", "video_id", "published"),
        ("wiki", "revid", "ts"),
        ("web", "result_id", "published"),
    ):
        # Search engines happily return years-old pages; keep only hits inside
        # the same lookback window as the passive feeds.
        extra = database._fresh(
            (found.get("items") or {}).get(stream) or [],
            stamp,
            config.SOURCE_LOOKBACK_DAYS,
        )
        existing = sources.get(stream) or []
        seen = {d.get(key) for d in existing}
        sources[stream] = list(existing) + [d for d in extra if d.get(key) not in seen]
    return found


def refresh_resources(
    top_n: int = 12,
    commit_limit: int = 3,
    min_weekly_coins: float = config.MIN_WEEKLY_VOLUME,
    persist: bool = True,
) -> dict[str, Any]:
    """Refresh cached evidence without running the AI debate.

    Snapshots Bazaar prices, polls every passive feed and runs the targeted
    per-ticker search. Backs the dashboard's "Update evidence resources" button.
    """
    database.init_db()
    warnings: list[str] = []

    payload = fetch_bazaar()
    products = normalize_products(payload)
    if not products:
        raise BazaarError("Bazaar returned zero products")
    inserted = database.record_snapshots(products) if persist else 0

    feeds = _collect_feeds(commit_limit, persist, warnings)
    sources = _cached_sources(feeds, persist)
    candidates = rank_candidates(products, limit=top_n, min_weekly_coins=min_weekly_coins)

    # Ask the planner what evidence to look for before the search runs, so the
    # targeted lookup collects the same material the gate will accept.
    plan = _collect(
        "Research planner",
        lambda: plan_research(candidates, "SHORT", database.calibration_stats(days=365), []),
        warnings,
    ) or {}
    found = _merge_active_search(
        candidates,
        sources,
        persist,
        warnings,
        query_map=plan.get("queries"),
        global_queries=plan.get("global_queries"),
    )
    calendar = _collect(
        "Update calendar",
        lambda: collect_update_calendar(sources, persist=persist),
        warnings,
    ) or {}

    if persist:
        database.set_setting("last_resource_refresh", database.utcnow())

    return {
        "refreshed_at": database.utcnow(),
        "products": len(products),
        "snapshots_saved": inserted,
        "candidates": len(candidates),
        "plan_mode": plan.get("mode"),
        "queries_planned": sum(len(v) for v in (plan.get("queries") or {}).values()),
        "calendar_events": len(calendar.get("upcoming") or calendar.get("events") or []),
        "feed_counts": {
            "github_leaks": len(feeds["github"].get("items") or []),
            "forum_posts": len(feeds["forum"].get("items") or []),
            "youtube_videos": len(feeds["youtube"].get("items") or []),
            "reddit_posts": len(feeds["reddit"].get("items") or []),
            "wiki_changes": len(feeds["wiki"].get("items") or []),
            "auction_stats": len(feeds["auction"].get("items") or []),
            "mayor": 1 if feeds["mayor"].get("snapshot") else 0,
        },
        "active_search": {
            "queries": int(found.get("queries") or 0),
            "reddit": len((found.get("items") or {}).get("reddit") or []),
            "youtube": len((found.get("items") or {}).get("youtube") or []),
            "wiki": len((found.get("items") or {}).get("wiki") or []),
            "web": len((found.get("items") or {}).get("web") or []),
        },
        "warnings": _compress_warnings(warnings),
    }


_NETWORK_MARKERS = (
    "no route to host",
    "max retries exceeded",
    "newconnectionerror",
    "connection refused",
    "connection reset",
    "temporary failure in name resolution",
    "timed out",
    "ssl",
)

_DOMAIN_RE = re.compile(r"https?://(?:www\.)?([^/\s`'\")]+)")
_FEED_NAMES = {
    "api.github.com": "GitHub",
    "hypixel.net": "Hypixel forums",
    "www.youtube.com": "YouTube",
    "www.reddit.com": "Reddit",
    "hypixel-skyblock.fandom.com": "Fandom wiki",
    "api.hypixel.net": "Hypixel API",
    "sky.coflnet.com": "Coflnet",
}


def _compress_warnings(warnings: list[str]) -> list[str]:
    """Collapse a wall of connection tracebacks into one readable line."""
    blocked: list[str] = []
    rate_limited: list[str] = []
    other: list[str] = []
    seen: set[str] = set()

    for raw in warnings:
        text = re.sub(r"\s+", " ", str(raw)).strip()
        lowered = text.lower()
        # Search-engine blocks and Reddit throttling are routine from this
        # network and the cache covers them, so they stay out of the banner.
        if "duckduckgo" in lowered:
            continue
        if "429" in text or "rate limit" in lowered:
            if "reddit" in lowered:
                continue
            if "youtube" in lowered:
                rate_limited.append("YouTube")
            else:
                rate_limited.append("a feed")
            continue
        if any(marker in lowered for marker in _NETWORK_MARKERS):
            match = _DOMAIN_RE.search(text)
            name = _FEED_NAMES.get(match.group(1)) if match else None
            if not name:
                name = text.split(":")[0].strip("` ")[:40] or "feed"
            if name == "Reddit":
                continue  # cached threads cover the gap
            blocked.append(name)
            continue
        if text in seen:
            continue
        seen.add(text)
        other.append(text[:200])

    compressed: list[str] = []
    if rate_limited:
        names = ", ".join(dict.fromkeys(rate_limited))
        compressed.append(f"Rate-limited (HTTP 429) by {names} — cached items were used where available.")
    if blocked:
        unique = list(dict.fromkeys(blocked))
        compressed.append(
            "Network egress blocked for "
            + ", ".join(unique)
            + " — using cached feed data for confluence scoring."
        )
    compressed.extend(other[:5])
    return compressed


def _run_term(
    term: str,
    candidates: list[dict],
    sources: dict[str, Any],
    threshold: dict,
    stats: dict,
    calibration: dict,
    upcoming: list[dict],
    persist: bool,
    warnings: list[str],
) -> dict[str, Any]:
    """One full pass: planner -> targeted search -> evidence gate -> debate."""
    plan: dict[str, Any] = {}
    if persist:
        plan = _collect(
            "Research planner",
            lambda: plan_research(candidates, term, calibration, upcoming),
            warnings,
        ) or {}
        if plan:
            database.save_research_plan(
                term,
                {
                    "term": term,
                    "summary": plan.get("summary"),
                    "criteria": plan.get("criteria"),
                    "queries": plan.get("queries"),
                    "global_queries": plan.get("global_queries"),
                    "mode": plan.get("mode"),
                },
            )

    found = _merge_active_search(
        candidates,
        sources,
        persist,
        warnings,
        query_map=plan.get("queries"),
        global_queries=plan.get("global_queries"),
    )

    evidence = _collect(
        "Evidence filter",
        lambda: filter_evidence(candidates, sources, plan.get("criteria")),
        warnings,
    ) or {}
    links = evidence.get("links") or {}
    filtered = evidence.get("sources") or sources
    scored = score_candidates(candidates, filtered, threshold, links)

    outcomes = database.recent_outcomes(limit=12) if persist else []
    analysis = analyze(
        scored,
        filtered,
        threshold,
        stats,
        outcomes,
        term=term,
        calibration=calibration,
        updates=upcoming,
    )
    warnings.extend(analysis.get("warnings") or [])
    return {
        "term": term,
        "plan": plan,
        "candidates": scored,
        "analysis": analysis,
        "evidence": evidence,
        "found": found,
    }


def _long_candidates(
    products: list[dict],
    upcoming: list[dict],
    min_weekly_coins: float,
    exclude: set[str],
) -> list[dict]:
    """Long universe: liquid items whose sector or id is in the update calendar."""
    if not upcoming:
        return []
    sectors = {
        str(s).upper()
        for e in upcoming
        for s in (e.get("sectors") or [])
        if str(s).upper() in LINKABLE_SECTORS
    }
    explicit = {str(i).upper() for e in upcoming for i in (e.get("items") or [])}
    pool = [
        p
        for p in products
        if p["weekly_volume_coins"] >= min_weekly_coins
        and abs(p.get("spread_pct") or 0) <= 12.0
        and p.get("sell_orders", 0) >= 1
        and p.get("buy_orders", 0) >= 1
        and p["sell_price"] > 0
        and p["mid_price"] > 0
        and (sector_for(p["item_id"]) in sectors or p["item_id"].upper() in explicit)
    ]
    if not pool:
        return []
    ranked = rank_candidates(
        pool,
        limit=config.LONG_MAX_CANDIDATES * 2,
        min_weekly_coins=min_weekly_coins,
    )
    preferred = [c for c in ranked if c["item_id"] not in exclude]
    return (preferred or ranked)[: config.LONG_MAX_CANDIDATES]


def _context_signals(signals: list[dict]) -> list[dict]:
    return [
        {
            "item_id": s.get("item_id"),
            "signal": s.get("signal"),
            "term": s.get("term"),
            "confidence": s.get("confidence"),
            "entry_price": s.get("entry_price"),
            "target_price": s.get("target_price"),
            "stop_price": s.get("stop_price"),
            "risk_ratio": s.get("risk_ratio"),
            "risk_tier": s.get("risk_tier"),
            "horizon_days": s.get("horizon_days"),
            "expected_days_to_target": s.get("expected_days_to_target"),
            "expected_target_date": s.get("expected_target_date"),
            "expected_return_pct": s.get("expected_return_pct"),
            "catalyst_date": s.get("catalyst_date"),
            "catalysts": (s.get("catalysts") or [])[:4],
            "thesis": str(s.get("thesis") or "")[:500],
            "confluence_score": s.get("confluence_score"),
            "sources": [str(x)[:80] for x in (s.get("sources") or [])[:4]],
        }
        for s in signals
    ]


def run_scan(
    top_n: int = 12,
    commit_limit: int = 3,
    min_weekly_coins: float = config.MIN_WEEKLY_VOLUME,
    send_alert: bool = True,
    persist: bool = True,
) -> dict[str, Any]:
    database.init_db()
    warnings: list[str] = []

    payload = fetch_bazaar()
    products = normalize_products(payload)
    if not products:
        raise BazaarError("Bazaar returned zero products")
    inserted = database.record_snapshots(products) if persist else 0
    price_map = {p["item_id"]: float(p.get("mid_price") or 0) for p in products}

    # Learn before predicting: grade every call whose forecast window has now
    # elapsed, then use the resulting calibration on this scan's confidence and
    # expected return date. This happens whether or not the user ever traded.
    grading = _collect(
        "Prediction grading",
        lambda: grade_predictions(price_map, persist=persist),
        warnings,
    ) or {}
    calibration = grading.get("calibration") or database.calibration_stats(days=365)

    # Cash-out alerts for the user's own holdings: edge-triggered take-profit /
    # stop messages on the dedicated portfolio webhook. Runs regardless of the
    # scan-alert toggle because missing an exit is worse than an extra push.
    portfolio_alerts = _collect(
        "Portfolio alerts",
        _portfolio_exit_alerts,
        warnings,
    ) or {"sent": 0, "triggered": []}

    stats = database.performance_stats(days=30)
    threshold = adaptive_threshold(stats)

    # ---- Alternative data feeds -------------------------------------------------
    feeds = _collect_feeds(commit_limit, persist, warnings)
    sources = _cached_sources(feeds, persist)

    # Update calendar: announced / predicted game updates mapped to sectors, so
    # long positions can be anchored to a real catalyst instead of a hunch.
    calendar = _collect(
        "Update calendar",
        lambda: collect_update_calendar(sources, persist=persist),
        warnings,
    ) or {}
    upcoming = calendar.get("upcoming") or calendar.get("events") or []

    # ---- SHORT pass: dip/flip, 1-3 days ---------------------------------------
    short_candidates = rank_candidates(
        products, limit=top_n, min_weekly_coins=min_weekly_coins
    )
    if not short_candidates:
        raise BazaarError("No liquid candidates passed the liquidity/spread filters")
    short_pass = _run_term(
        "SHORT",
        short_candidates,
        sources,
        threshold,
        stats,
        calibration,
        upcoming,
        persist,
        warnings,
    )

    # ---- LONG pass: catalyst-driven, 1-4 weeks --------------------------------
    long_pass: dict[str, Any] = {}
    long_candidates = _long_candidates(
        products,
        upcoming,
        min_weekly_coins,
        {c["item_id"] for c in short_candidates},
    )
    if long_candidates:
        # Long ideas need a deeper evidence window: updates are discussed for
        # weeks before they land.
        long_sources = _cached_sources(
            feeds, persist, lookback_days=config.LONG_EVIDENCE_LOOKBACK_DAYS
        )
        long_pass = _run_term(
            "LONG",
            long_candidates,
            long_sources,
            threshold,
            stats,
            calibration,
            upcoming,
            persist,
            warnings,
        )

    runs = [short_pass] + ([long_pass] if long_pass else [])
    signals = [s for run in runs for s in run["analysis"]["signals"]]
    long_signals = [s for s in signals if (s.get("term") or "SHORT") == "LONG"]

    resolved_count = 0
    stored = 0
    report: dict[str, Any] = {}
    if persist:
        resolved = _resolve_open_signals(price_map)
        resolved_count = len(resolved)
        stats = database.performance_stats(days=30)

        holds = {"SHORT": 0, "LONG": 0}
        seen_open = _recent_open_items()
        for signal in signals:
            term = (signal.get("term") or "SHORT").upper()
            if signal["confidence"] < config.AI_CONFIDENCE_FLOOR:
                continue
            key = (signal["item_id"], term)
            if key in seen_open:
                continue
            # Watch notes are useful but must not drown the book: keep the two
            # most relevant HOLDs per term, and they expire after a day.
            if signal["signal"] == "HOLD":
                if holds[term] >= 2:
                    continue
                holds[term] += 1
            database.save_signal(signal)
            stored += 1
            seen_open.add(key)

        database.set_setting("last_scan_at", database.utcnow())
        database.set_setting("last_market_view", short_pass["analysis"].get("market_view", ""))
        database.set_setting("last_bull_overview", short_pass["analysis"].get("bull_overview", ""))
        database.set_setting("last_bear_overview", short_pass["analysis"].get("bear_overview", ""))
        database.set_setting("last_scan_source", short_pass["analysis"].get("source", ""))
        database.set_setting("last_threshold", f"{threshold['buy_threshold']:.2f}")
        if long_pass:
            database.set_setting(
                "last_market_view_long", long_pass["analysis"].get("market_view", "")
            )
            database.set_setting(
                "last_bull_overview_long", long_pass["analysis"].get("bull_overview", "")
            )
            database.set_setting(
                "last_bear_overview_long", long_pass["analysis"].get("bear_overview", "")
            )
        database.set_setting(
            "last_update_calendar",
            json.dumps(
                {
                    "generated_at": database.utcnow(),
                    "mode": calendar.get("mode"),
                    "sectors": calendar.get("sectors") or [],
                    "events": (upcoming or [])[:20],
                },
                ensure_ascii=False,
                default=str,
            ),
        )
        database.set_setting(
            "last_calibration",
            json.dumps(calibration, default=str),
        )
        database.set_setting(
            "last_grading",
            json.dumps(grading, default=str),
        )
        database.set_setting(
            "last_portfolio_alerts",
            json.dumps({**portfolio_alerts, "at": database.utcnow()}, default=str),
        )
        # Snapshot of everything the follow-up consultant needs, so asking a
        # question does not require another scan.
        database.set_setting(
            "last_analysis_context",
            json.dumps(
                {
                    "scanned_at": database.utcnow(),
                    "source": short_pass["analysis"].get("source", ""),
                    "market_view": short_pass["analysis"].get("market_view", ""),
                    "bull_overview": short_pass["analysis"].get("bull_overview", ""),
                    "bear_overview": short_pass["analysis"].get("bear_overview", ""),
                    "market_view_long": (long_pass.get("analysis") or {}).get("market_view", ""),
                    "bull_overview_long": (long_pass.get("analysis") or {}).get("bull_overview", ""),
                    "bear_overview_long": (long_pass.get("analysis") or {}).get("bear_overview", ""),
                    "threshold": threshold,
                    "track_record": stats,
                    "calibration": calibration,
                    "updates": (upcoming or [])[:10],
                    "signals": _context_signals(signals),
                    "candidates": [
                        {
                            "item_id": c.get("item_id"),
                            "mid_price": c.get("mid_price"),
                            "risk_ratio": c.get("risk_ratio"),
                            "dip_z": c.get("dip_z"),
                            "ma7": c.get("ma7"),
                            "weekly_volume_coins": c.get("weekly_volume_coins"),
                            "sector": c.get("sector"),
                            "confluence_score": c.get("confluence_score"),
                            "risk_tier": c.get("risk_tier"),
                            "evidence_count": c.get("evidence_count"),
                        }
                        for c in short_candidates
                    ],
                },
                ensure_ascii=False,
                default=str,
            ),
        )

        # Written long-term due diligence: why each catalyst position exists.
        if long_pass:
            report = _collect(
                "Long-term report",
                lambda: _write_long_report(long_signals, upcoming, calibration),
                warnings,
            ) or {}
            if report:
                database.set_setting("last_long_report", json.dumps(report, default=str))

    alert_sent = False
    if send_alert and persist and signals:
        import alerts

        alert_sent = alerts.push_signals(
            signals,
            short_pass["analysis"].get("market_view", ""),
            short_pass["analysis"].get("source", ""),
        )

    short_evidence = short_pass["evidence"].get("stats") or {}
    long_evidence = (long_pass.get("evidence") or {}).get("stats") or {}
    return {
        "scanned_at": database.utcnow(),
        "products": len(products),
        "snapshots_saved": inserted,
        "candidates": short_candidates,
        "signals": signals,
        "short_signals": [s for s in signals if (s.get("term") or "SHORT") == "SHORT"],
        "long_signals": long_signals,
        "market_view": short_pass["analysis"].get("market_view", ""),
        "bull_overview": short_pass["analysis"].get("bull_overview", ""),
        "bear_overview": short_pass["analysis"].get("bear_overview", ""),
        "market_view_long": (long_pass.get("analysis") or {}).get("market_view", ""),
        "source": short_pass["analysis"]["source"],
        "model": short_pass["analysis"]["model"],
        "modes": [m for run in runs for m in (run["analysis"].get("modes") or [])],
        "threshold": threshold,
        "stats": stats,
        "calibration": calibration,
        "grading": grading,
        "upcoming_updates": upcoming,
        "update_calendar_mode": calendar.get("mode"),
        "research_plans": {
            run["term"]: {
                "mode": (run.get("plan") or {}).get("mode"),
                "summary": (run.get("plan") or {}).get("summary"),
                "queries": (run.get("plan") or {}).get("queries") or {},
            }
            for run in runs
        },
        "report": report,
        "stored": stored,
        "resolved": resolved_count,
        "alert_sent": alert_sent,
        "portfolio_alerts": portfolio_alerts,
        "warnings": _compress_warnings(warnings),
        "evidence_filter": short_evidence,
        "evidence_filter_long": long_evidence,
        "active_search": {
            "queries": int((short_pass.get("found") or {}).get("queries") or 0),
            "reddit": len(((short_pass.get("found") or {}).get("items") or {}).get("reddit") or []),
            "youtube": len(((short_pass.get("found") or {}).get("items") or {}).get("youtube") or []),
            "wiki": len(((short_pass.get("found") or {}).get("items") or {}).get("wiki") or []),
            "web": len(((short_pass.get("found") or {}).get("items") or {}).get("web") or []),
        },
        "feed_counts": {
            "github_leaks": len(feeds["github"].get("items") or []),
            "forum_posts": len(feeds["forum"].get("items") or []),
            "youtube_videos": len(feeds["youtube"].get("items") or []),
            "reddit_posts": len(feeds["reddit"].get("items") or []),
            "wiki_changes": len(feeds["wiki"].get("items") or []),
            "auction_stats": len(feeds["auction"].get("items") or []),
            "mayor": 1 if feeds["mayor"].get("snapshot") else 0,
        },
    }


def _write_long_report(
    signals: list[dict], upcoming: list[dict], calibration: dict
) -> dict[str, Any]:
    """Build the long-term due-diligence PDF and markdown."""
    from reports.long_report import build_long_report

    return build_long_report(signals, upcoming, calibration)
