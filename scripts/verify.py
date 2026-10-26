"""Layer-by-layer smoke test against the live data feeds and Cerebras.

Run from the project root:
    python scripts/verify.py           # checks only
    python scripts/verify.py --full    # also runs the full 3-agent AI scan
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv

load_dotenv("env.txt", override=False)

import config  # noqa: E402
import database  # noqa: E402
from analytics import allocation_plan, rank_candidates, score_candidates, sector_for  # noqa: E402
from ingestion import (  # noqa: E402
    collect_forum_posts,
    collect_new_leaks,
    collect_reddit_posts,
    collect_wiki_changes,
    collect_youtube_videos,
    fetch_bazaar,
    normalize_products,
)
from ingestion.calendar_mayor import collect_mayor_context  # noqa: E402
from ingestion.coflnet_ah import fetch_item_metadata, fetch_lbin  # noqa: E402
from ingestion.github_watcher import fetch_commit_detail, fetch_recent_commits  # noqa: E402
from intelligence import ping  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []
_state: dict = {}


def check(name: str, fn) -> None:
    started = time.perf_counter()
    try:
        detail = fn()
        ms = int((time.perf_counter() - started) * 1000)
        results.append((PASS, name, f"{detail} ({ms} ms)"))
    except Exception as exc:
        ms = int((time.perf_counter() - started) * 1000)
        results.append((FAIL, name, f"{type(exc).__name__}: {exc} ({ms} ms)"))


def _feed_summary(result: dict) -> str:
    items = result.get("items") or []
    errors = result.get("errors") or []
    text = f"{len(items)} new item(s)"
    if errors:
        text += f", degraded: {'; '.join(str(e)[:90] for e in errors)}"
    return text


def check_env() -> str:
    assert config.HYPIXEL_API_KEY, "HYPIXEL_API_KEY missing"
    assert config.CEREBRAS_API_KEY, "CEREBRAS_API_KEY missing"
    assert config.CEREBRAS_BASE_URL == "https://api.cerebras.ai/v1", config.CEREBRAS_BASE_URL
    assert config.CEREBRAS_MODEL == "gpt-oss-120b", config.CEREBRAS_MODEL
    assert config.DB_PATH.exists(), "database file not created"
    return (
        f"keys hypixel={config.mask(config.HYPIXEL_API_KEY)} "
        f"cerebras={config.mask(config.CEREBRAS_API_KEY)} model={config.CEREBRAS_MODEL}"
    )


def check_schema() -> str:
    with database.connection() as conn:
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    required = {
        "market_snapshots", "signals", "leaks", "portfolios", "holdings", "model_runs",
        "forum_posts", "youtube_videos", "reddit_posts", "auction_stats", "mayor_cycles",
        "wiki_changes", "settings",
    }
    missing = required - tables
    assert not missing, f"missing tables: {sorted(missing)}"
    return f"{len(tables)} tables, all {len(required)} required tables present"


def check_bazaar() -> str:
    payload = fetch_bazaar()
    rows = normalize_products(payload)
    assert len(rows) > 100, f"only {len(rows)} products"
    saved = database.record_snapshots(rows)
    top = max(rows, key=lambda r: r["weekly_volume_coins"])
    _state["products"] = rows
    return f"{len(rows):,} products, {saved} new snapshot rows, top volume {top['item_id']}"


def check_candidates() -> str:
    rows = _state.get("products")
    if not rows:
        check_bazaar()
        rows = _state["products"]
    candidates = rank_candidates(rows, limit=12)
    assert candidates, "no candidates passed liquidity filters"
    _state["candidates"] = candidates
    return (
        f"{len(candidates)} candidates, best {candidates[0]['item_id']} "
        f"(score {candidates[0]['composite_score']})"
    )


def check_confluence() -> str:
    candidates = _state.get("candidates")
    if not candidates:
        check_candidates()
        candidates = _state["candidates"]
    item = candidates[0]["item_id"]
    sources = {
        "github": [{"sha": "verify", "title": f"{item} recipe leak", "items": [item], "diff": ""}],
        "forum": [{"title": f"{item} changes", "summary": "", "feed": "SkyBlock Alpha"}],
        "youtube": [{"title": f"{item} guide", "channel": "Verify", "transcript": "test"}],
        "reddit": [{"title": f"{item} hype", "body": "", "score": 120}],
        "auction": [],
        "wiki": [],
        "macro": {},
    }
    scored = score_candidates([candidates[0]], sources, {"buy_threshold": 0.92})[0]
    assert scored["evidence_count"] >= 2, scored
    assert scored["confluence_score"] > 0.5, scored
    _state["scored"] = scored
    return (
        f"{item} confluence {scored['confluence_score']:.2f} "
        f"tier {scored['risk_tier']} streams {scored['evidence_count']}"
    )


def check_github() -> str:
    commits = fetch_recent_commits(limit=1)
    assert commits, "no commits returned"
    sha = commits[0]["sha"]
    detail = fetch_commit_detail(sha)
    files = len(detail.get("files") or [])
    title = (detail.get("commit", {}).get("message") or "").split("\n")[0][:60]
    _state["sha"] = sha
    return f"{sha[:8]} files={files} '{title}'"


def check_leaks() -> str:
    leaks = collect_new_leaks(commit_limit=2)
    return f"{len(leaks)} new leak(s) cached"


def check_forum() -> str:
    return _feed_summary(collect_forum_posts())


def check_youtube() -> str:
    return _feed_summary(collect_youtube_videos())


def check_reddit() -> str:
    return _feed_summary(collect_reddit_posts())


def check_wiki() -> str:
    return _feed_summary(collect_wiki_changes())


def check_coflnet() -> str:
    metadata = fetch_item_metadata()
    assert len(metadata) > 1000, f"only {len(metadata)} item metadata rows"
    sample = "ENCHANTED_DIAMOND"
    row = fetch_lbin(sample)
    assert row, f"no LBIN stats for {sample}"
    return f"{len(metadata):,} item tags, {sample} median {row['median']}"


def check_mayor() -> str:
    result = collect_mayor_context()
    snapshot = result.get("snapshot")
    if not snapshot:
        raise AssertionError("; ".join(result.get("errors") or ["no election snapshot"]))
    return (
        f"{snapshot.get('mayor_name')} · year {snapshot.get('election_year')} "
        f"· {len(snapshot.get('perks') or [])} perks"
    )


def check_cerebras() -> str:
    response = ping()
    assert response.get("ok") is True, response
    return f"{config.CEREBRAS_MODEL} replied {response}"


def check_analytics() -> str:
    plan = allocation_plan(50_000_000, max_position_pct=0.15, reserve_pct=0.10, active_slots=4)
    assert abs(plan["max_per_trade"] - 6_750_000) < 1, plan
    scaled = allocation_plan(50_000_000, accuracy_multiplier=1.2)
    assert scaled["max_per_trade"] > plan["max_per_trade"], scaled
    assert sector_for("ENCHANTED_DIAMOND") == "MINING"
    assert sector_for("ENCHANTED_POTATO") == "FARMING"
    return (
        f"50M purse -> {plan['max_per_trade']:,.0f}/trade, "
        f"{plan['reserve']:,.0f} reserve, accuracy scaling + sectors ok"
    )


def check_feedback() -> str:
    signal_id = database.save_signal(
        {
            "item_id": "VERIFY_TEST_ITEM",
            "item_name": "Verify Test Item",
            "signal": "BUY",
            "confidence": 0.9,
            "entry_price": 100.0,
            "target_price": 120.0,
            "stop_price": 90.0,
            "risk_ratio": 0.9,
            "ma7": 111.0,
            "sector": "OTHER",
            "horizon_days": 7,
            "catalysts": ["verification"],
            "thesis": "Synthetic row created by scripts/verify.py",
            "source": "test",
            "sources": ["Verify: synthetic"],
            "confluence_score": 0.7,
            "risk_tier": "HIGH",
            "bull_case": "synthetic bull",
            "bear_case": "synthetic bear",
        }
    )
    outcome = database.resolve_signal(signal_id, 121.0)
    assert outcome["status"] == "won", outcome
    stats = database.performance_stats(days=30)
    assert stats["closed"] >= 1, stats
    with database.connection() as conn:
        conn.execute("DELETE FROM signals WHERE id = ?", (signal_id,))
    return f"insert/resolve w/ cleanup ok, pnl {outcome['pnl_pct']:+.2f}%, win-rate {stats['win_rate']:.0%}"


def check_daemon() -> str:
    import daemon

    assert callable(daemon.run_cycle), "daemon.run_cycle missing"
    return "daemon.run_cycle present (asyncio/APScheduler)"


def check_compile() -> str:
    import py_compile

    files = ["app.py", "pipeline.py", "daemon.py", "database.py", "config.py", "alerts.py"]
    files += [str(p) for p in ROOT.glob("ingestion/*.py")]
    files += [str(p) for p in ROOT.glob("intelligence/*.py")]
    files += [str(p) for p in ROOT.glob("analytics/*.py")]
    files += [str(p) for p in ROOT.glob("ui/*.py")]
    files += [str(p) for p in ROOT.glob("scripts/*.py")]
    for f in files:
        py_compile.compile(f, doraise=True)
    return f"{len(files)} modules compile"


def run_full_scan() -> str:
    import pipeline

    result = pipeline.run_scan(top_n=12, commit_limit=2, send_alert=False)
    assert result["signals"], "scan produced no signals"
    return (
        f"{result['products']:,} products -> {len(result['signals'])} signals "
        f"({result['source']}, modes {result.get('modes')}), stored {result['stored']}, "
        f"threshold {result['threshold']['buy_threshold']:.3f}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="run a full AI scan")
    args = parser.parse_args()

    database.init_db()
    check("Environment (env.txt)", check_env)
    check("SQLite schema", check_schema)
    check("Hypixel Bazaar feed", check_bazaar)
    check("Risk ranking / MA7", check_candidates)
    check("Confluence matrix", check_confluence)
    check("GitHub NEU feed", check_github)
    check("Leak collector", check_leaks)
    check("Forum RSS feeds", check_forum)
    check("YouTube scanner", check_youtube)
    check("Reddit scanner", check_reddit)
    check("Wiki mediawiki", check_wiki)
    check("Coflnet AH / LBIN", check_coflnet)
    check("Mayor election", check_mayor)
    check("Cerebras gpt-oss-120b", check_cerebras)
    check("Analytics maths", check_analytics)
    check("Feedback loop", check_feedback)
    check("Daemon module", check_daemon)
    check("Module compilation", check_compile)
    if args.full:
        check("Full AI debate scan", run_full_scan)

    width = max(len(n) for _, n, _ in results) + 2
    print()
    for status, name, detail in results:
        print(f"[{status}] {name.ljust(width)} {detail}")
    failures = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{len(results) - failures}/{len(results)} checks passed")
    if failures:
        print("Some checks failed - see details above.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
