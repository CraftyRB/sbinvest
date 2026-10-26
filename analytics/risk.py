"""Decision and risk engine.

The core valuation metric is the dip ratio `current_price / 7D_moving_average`.
Buy thresholds adapt to the live win-rate stored in SQLite, so the scanner gets
more conservative after a losing streak and more aggressive after a hot streak.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime
from typing import Any, Callable

import config
import database


def moving_average(item_id: str, days: int = 7) -> tuple[float, int, int, float, float, float]:
    """Return (ma, samples, distinct_days, momentum_pct, span_hours, stdev_pct).

    The span matters because a fresh install only has same-day snapshots. A
    multi-day average and a six-hour average are both useful references, but
    they must not be treated as equally reliable. `stdev_pct` turns a raw dip
    ratio into a statistical one: a 0.3% dip is huge for a flat item and noise
    for a volatile one.
    """
    history = database.price_history(item_id, days=days)
    if not history:
        return 0.0, 0, 0, 0.0, 0.0, 0.0
    prices = [p for _, p in history]
    ma = sum(prices) / len(prices)
    distinct_days = len({ts[:10] for ts, _ in history})
    first, last = prices[0], prices[-1]
    momentum = ((last / first) - 1) * 100 if first else 0.0
    try:
        start = datetime.fromisoformat(history[0][0])
        end = datetime.fromisoformat(history[-1][0])
        span_hours = (end - start).total_seconds() / 3600.0
    except ValueError:
        span_hours = 0.0
    stdev_pct = (statistics.pstdev(prices) / ma * 100) if ma else 0.0
    return ma, len(prices), distinct_days, momentum, span_hours, stdev_pct


def adaptive_threshold(stats: dict[str, Any], base: float | None = None) -> dict[str, Any]:
    """Shift the buy threshold by recent performance.

    Win-rate 50% keeps the base threshold. A 70% win-rate loosens it by ~2.4%
    (buy earlier); a 30% win-rate tightens it by ~2.4% (demand a deeper dip).
    Below 3 closed BUY/SELL calls the threshold stays at the base.
    """
    base = config.BASE_BUY_THRESHOLD if base is None else base
    win_rate = float(stats.get("win_rate", 0.5))
    sample = int(stats.get("closed", 0))
    # Hold the base threshold until a few real BUY/SELL outcomes exist: one or
    # two closed calls can read 0% or 100% and would swing the threshold on noise.
    if sample < 3:
        adjustment = 0.0
    else:
        adjustment = (win_rate - 0.5) * 0.12 * (1.0 if sample >= 5 else 0.4)
    threshold = max(0.80, min(0.98, base + adjustment))
    return {
        "buy_threshold": round(threshold, 4),
        "base_threshold": base,
        "win_rate": win_rate,
        "sample_size": sample,
        "adjustment": round(adjustment, 4),
    }


def qualifies_buy(candidate: dict, buy_threshold: float | None = None) -> bool:
    """Single source of truth for the quantitative BUY gate.

    A multi-day reference uses the adaptive price/MA threshold. A same-day
    reference demands both a tangible discount and a statistical one: the price
    must be below `INTRADAY_BUY_THRESHOLD` *and* at least `INTRADAY_Z_BUY`
    standard deviations below its own mean, so an item that is merely flat
    cannot pass on a 0.3% wobble. Near-flat items (stdev below 0.05%) skip the
    z check because their sigma is not measurable.
    """
    if not candidate.get("has_price_ref"):
        return False
    ratio = float(candidate.get("risk_ratio", 1.0))
    dip_z = float(candidate.get("dip_z") or 0.0)
    if candidate.get("has_history"):
        threshold = float(
            buy_threshold if buy_threshold is not None else config.BASE_BUY_THRESHOLD
        )
        if ratio <= threshold:
            return True
        # A sharp same-day dislocation inside an otherwise normal range is
        # still tradeable when it is statistically extreme.
        return ratio <= config.INTRADAY_BUY_THRESHOLD and dip_z <= -1.25
    if ratio > config.INTRADAY_BUY_THRESHOLD:
        return False
    if float(candidate.get("volatility_pct") or 0.0) < 0.05:
        return True
    return dip_z <= config.INTRADAY_Z_BUY


def rank_candidates(
    products: list[dict],
    limit: int = 12,
    min_weekly_coins: float = config.MIN_WEEKLY_VOLUME,
    days: int = 7,
    max_spread_pct: float = 12.0,
) -> list[dict]:
    """Filter for liquidity, enrich with MA7, and shortlist opportunities.

    The shortlist is deliberately two-bucketed: half the slots go to the
    deepest dips (so undervaluation is never crowded out by megacap volume) and
    half to the most liquid names (so there is always something tradeable and
    well-covered by the evidence feeds).
    """
    candidates: list[dict] = []
    for p in products:
        if p["weekly_volume_coins"] < min_weekly_coins:
            continue
        # Reject both wide and inverted books: a negative spread means the top
        # ask is below the top bid (thin spoof orders), not a real opportunity.
        if abs(p["spread_pct"]) > max_spread_pct:
            continue
        if p.get("sell_orders", 0) < 1 or p.get("buy_orders", 0) < 1:
            continue
        if p["sell_price"] <= 0 or p["mid_price"] <= 0:
            continue

        ma, samples, distinct_days, momentum, span_hours, stdev_pct = moving_average(
            p["item_id"], days=days
        )
        # Two distinct calendar days make a real MA7. Until then, a same-day
        # baseline still works for flips as long as the snapshots span a few
        # hours; it is just scored with a reliability discount.
        has_history = samples >= 2 and distinct_days >= 2 and ma > 0
        has_intraday = not has_history and samples >= 4 and span_hours >= 2.0 and ma > 0
        has_price_ref = has_history or has_intraday
        ratio = (p["mid_price"] / ma) if has_price_ref else 1.0
        # Statistical dip: how many sigma below the recent mean the price sits.
        # Clamped because thin books throw absurd outliers that would dominate
        # any ranking.
        dip_z = 0.0
        if has_price_ref and stdev_pct >= 0.05:
            dip_z = max(-4.0, min(4.0, (ratio - 1.0) * 100 / stdev_pct))

        liquidity_score = math.log10(max(p["weekly_volume_coins"], 1.0))
        spread_penalty = abs(p["spread_pct"]) * 0.05
        score = liquidity_score + 3.0 * max(0.0, -dip_z) - spread_penalty

        candidates.append(
            {
                **p,
                "ma7": round(ma, 2) if ma else None,
                "samples": samples,
                "horizon_days": distinct_days,
                "has_history": has_history,
                "has_intraday": has_intraday,
                "has_price_ref": has_price_ref,
                "span_hours": round(span_hours, 1),
                "risk_ratio": round(ratio, 4),
                "dip_z": round(dip_z, 2),
                "volatility_pct": round(stdev_pct, 3),
                "momentum_pct": round(momentum, 2),
                "composite_score": round(score, 2),
            }
        )

    if limit <= 0 or not candidates:
        return []

    # Bucket 1: deepest statistical dips, with a volume floor so the shortlist
    # stays fillable.
    floor = max(min_weekly_coins, config.DIP_VOLUME_FLOOR)
    dip_pool = [
        c for c in candidates if c["has_price_ref"] and c["weekly_volume_coins"] >= floor
    ]
    dip_pool.sort(key=lambda c: (c["risk_ratio"], c["dip_z"]))
    n_dip = max(1, limit // 2)
    picks: list[dict] = dip_pool[:n_dip]

    # Bucket 2: the most liquid names, which also carry the best evidence
    # coverage.
    if len(picks) < limit:
        chosen = {c["item_id"] for c in picks}
        for c in sorted(candidates, key=lambda c: c["weekly_volume_coins"], reverse=True):
            if len(picks) >= limit:
                break
            if c["item_id"] not in chosen:
                chosen.add(c["item_id"])
                picks.append(c)
    picks.sort(key=lambda c: (c["risk_ratio"], c["dip_z"]))
    return picks[:limit]


def estimate_exit_plan(price: float, signal: str = "BUY", volatility_pct: float = 0.0) -> dict:
    """Default target/stop scaffolding when the model omits price levels."""
    target_pct = max(0.10, min(0.35, 0.12 + volatility_pct / 100))
    stop_pct = 0.07
    if signal.upper() == "SELL":
        return {"target_price": round(price * 0.88, 2), "stop_price": round(price * 1.06, 2)}
    return {
        "target_price": round(price * (1 + target_pct), 2),
        "stop_price": round(price * (1 - stop_pct), 2),
    }
