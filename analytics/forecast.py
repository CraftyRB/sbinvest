"""Expected time-to-target forecasting.

A prediction is only useful if it says *when*. The model here converts a
price target into an expected number of days using the item's own observed
volatility (first-passage scaling), then bends that estimate toward a known
or predicted update date when one exists.

Every forecast is later graded by `analytics.grading`; the realised
days-to-target ratio is folded back into `FORECAST_TIME_SCALE`, so repeated
over- or under-estimation self-corrects without the user placing a trade.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

import config
import database


def time_scale() -> float:
    """Learned multiplier applied to every raw day estimate."""
    try:
        value = float(database.get_setting("forecast_time_scale", "1.0") or 1.0)
    except (TypeError, ValueError):
        value = 1.0
    return max(0.55, min(2.20, value))


def _daily_sigma(volatility_pct: float, samples: int, span_hours: float) -> float:
    """Per-day percentage sigma from the observed window's stdev.

    `volatility_pct` is the population stdev of prices over the sampled window,
    expressed as a percent of the mean. Dividing by sqrt(window days) turns a
    window stdev into a rough daily stdev for a random walk.
    """
    window_days = (span_hours / 24.0) if span_hours and span_hours > 1 else max(1.0, samples)
    sigma = abs(volatility_pct) / 100.0 / math.sqrt(max(1.0, window_days))
    return max(0.006, min(0.18, sigma))


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def estimate_days(
    entry: float,
    target: float,
    *,
    volatility_pct: float = 0.0,
    momentum_pct: float = 0.0,
    samples: int = 0,
    span_hours: float = 0.0,
    term: str = "SHORT",
    catalyst_date: str | None = None,
    catalyst_confidence: float = 0.0,
) -> dict[str, Any]:
    """Return {expected_days, expected_date, method, catalyst_date, scale}."""
    entry = float(entry or 0)
    target = float(target or 0)
    term = (term or "SHORT").upper()
    scale = time_scale()
    if entry <= 0 or target <= 0:
        days = config.SHORT_HORIZON_DAYS if term == "SHORT" else config.LONG_HORIZON_DAYS
        return {
            "expected_days": int(round(days)),
            "expected_date": (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat(),
            "method": "default",
            "catalyst_date": catalyst_date or "",
            "scale": scale,
        }

    required = max(0.004, abs(target / entry - 1.0))
    sigma = _daily_sigma(volatility_pct, samples, span_hours)
    # First-passage scaling: a random walk of daily sigma typically covers a
    # distance d in about (d / sigma)^2 days. It is a lower bound on the mean
    # hit time, which is why the learned time scale can stretch it later.
    base_days = (required / sigma) ** 2

    # Positive momentum means the move is already underway; negative momentum
    # means the gap is widening and will take longer to close.
    if momentum_pct > 0:
        base_days *= max(0.55, 1.0 - min(0.45, momentum_pct / 20.0))
    elif momentum_pct < 0:
        base_days *= min(1.60, 1.0 + abs(momentum_pct) / 25.0)

    method = "volatility"
    catalyst_day = _parse_date(catalyst_date)
    if catalyst_day:
        today = datetime.now(timezone.utc).date()
        catalyst_days = (catalyst_day - today).days
        if catalyst_days < 0:
            catalyst_days = 1  # update is live / just released
        # Supply and demand react within a couple of days of an update going
        # live, so anchor near the catalyst and keep some diffusion weight.
        weight = 0.35 + 0.45 * max(0.0, min(1.0, catalyst_confidence))
        base_days = base_days * (1 - weight) + max(1.0, catalyst_days + 2) * weight
        method = "catalyst"

    base_days *= scale
    if term == "SHORT":
        days = max(0.5, min(float(config.SHORT_HORIZON_DAYS), base_days))
    else:
        days = max(float(config.LONG_MIN_HORIZON_DAYS), min(float(config.LONG_HORIZON_DAYS), base_days))

    expected = datetime.now(timezone.utc) + timedelta(days=days)
    return {
        "expected_days": int(max(1, round(days))),
        "expected_date": expected.date().isoformat(),
        "method": method,
        "catalyst_date": catalyst_date or "",
        "scale": round(scale, 3),
    }


def expected_return_pct(entry: float, target: float) -> float:
    if not entry:
        return 0.0
    return round((float(target) / float(entry) - 1.0) * 100.0, 2)


def catalyst_days(catalyst_date: str | None) -> int | None:
    parsed = _parse_date(catalyst_date)
    if not parsed:
        return None
    return (parsed - datetime.now(timezone.utc).date()).days
