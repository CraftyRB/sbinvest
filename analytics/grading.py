"""Self-learning prediction grading.

The desk grades its own calls at their stated horizon whether or not a user
ever placed the trade. Each grade records what was predicted, what actually
happened, and how long the move really took; the aggregate calibrates the
forecast time scale and the confidence multiplier used on the next scan.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import database


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _first_hit(
    signal: dict, created: datetime, horizon_end: datetime
) -> tuple[int, float] | None:
    """First snapshot inside the horizon where the target was actually reached."""
    kind = (signal.get("signal") or "").upper()
    target = float(signal.get("target_price") or 0)
    if target <= 0:
        return None
    days_window = max(4, int((datetime.now(timezone.utc) - created).days) + 3)
    history = database.price_history(signal["item_id"], days=days_window)
    for ts, price in history:
        stamp = _parse(ts)
        if not stamp or stamp < created or stamp > horizon_end + timedelta(days=2):
            continue
        if kind == "BUY" and price >= target:
            return max(0, (stamp - created).days), price
        if kind == "SELL" and price <= target:
            return max(0, (stamp - created).days), price
    return None


def _grade_one(signal: dict, price: float, created: datetime) -> dict[str, Any]:
    kind = (signal.get("signal") or "").upper()
    entry = float(signal.get("entry_price") or 0)
    target = float(signal.get("target_price") or 0)
    stop = float(signal.get("stop_price") or 0)
    actual_return = ((price / entry) - 1.0) * 100.0 if entry else 0.0

    if kind == "BUY":
        if target and price >= target:
            grade, accuracy = "accurate", 1.0
            note = "Target reached within the forecast window."
        elif stop and price <= stop:
            grade, accuracy = "inaccurate", 0.0
            note = "Stop level was hit before the target."
        elif target and price >= entry + 0.5 * (target - entry):
            grade, accuracy = "partially_accurate", 0.5
            note = "Right direction and over half the move, but the target was missed."
        else:
            grade, accuracy = "inaccurate", 0.0
            note = "Price did not make meaningful progress toward the target."
    elif kind == "SELL":
        lower = min(target, entry) if target else entry
        if target and price <= lower:
            grade, accuracy = "accurate", 1.0
            note = "Downside target reached."
        elif price < entry:
            grade, accuracy = "partially_accurate", 0.5
            note = "Direction correct, target not reached."
        else:
            grade, accuracy = "inaccurate", 0.0
            note = "Price held or rose against the sell call."
    else:
        grade, accuracy, note = "inaccurate", 0.0, "HOLD calls are not graded."

    return {
        "grade": grade,
        "accuracy": accuracy,
        "note": note,
        "actual_return_pct": round(actual_return, 2),
        "actual_days": round((datetime.now(timezone.utc) - created).total_seconds() / 86400.0, 2),
    }


def grade_predictions(
    price_map: dict[str, float] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Grade every due, still-ungraded prediction; then recalibrate."""
    price_map = price_map or {}
    due = database.signals_due_for_grading()
    graded = 0
    now = database.utcnow()

    for signal in due:
        created = _parse(signal.get("created_at"))
        if not created:
            continue
        predicted_days = int(signal.get("expected_days_to_target") or signal.get("horizon_days") or 14)
        horizon_end = created + timedelta(days=predicted_days)
        hit = _first_hit(signal, created, horizon_end)

        price = price_map.get(signal["item_id"])
        if not price:
            price = database.price_at_or_before(signal["item_id"], horizon_end.isoformat(timespec="seconds"))
        if not price:
            history = database.price_history(signal["item_id"], days=max(4, predicted_days + 7))
            price = history[-1][1] if history else None
        if not price:
            continue

        result = _grade_one(signal, float(price), created)
        if hit is None and result["grade"] == "accurate":
            # The level was reached, but only after the stated forecast window:
            # the direction was right and the timing was wrong, so it scores as
            # partial rather than a clean win.
            result["grade"] = "partially_accurate"
            result["accuracy"] = 0.5
            result["note"] = "Target reached, but after the forecast window (late call)."
        actual_days = hit[0] if hit else result["actual_days"]
        record = {
            "signal_id": signal["id"],
            "term": (signal.get("term") or "SHORT").upper(),
            "item_id": signal["item_id"],
            "signal": signal.get("signal"),
            "confidence": signal.get("confidence"),
            "created_at": signal.get("created_at"),
            "graded_at": now,
            "horizon_end": horizon_end.isoformat(timespec="seconds"),
            "predicted_days": predicted_days,
            "actual_days": actual_days,
            "predicted_target": signal.get("target_price"),
            "predicted_return_pct": signal.get("expected_return_pct"),
            "actual_price": round(float(price), 2),
            "actual_return_pct": result["actual_return_pct"],
            "grade": result["grade"],
            "accuracy": result["accuracy"],
            "note": result["note"],
        }
        if persist:
            database.save_prediction_grade(record)
        graded += 1

    calibration = database.calibration_stats(days=365)
    scale = _learn_time_scale(calibration)
    if persist:
        database.record_calibration(calibration, scale)
    return {
        "graded": graded,
        "calibration": calibration,
        "time_scale": scale,
    }


def _learn_time_scale(calibration: dict[str, Any]) -> float:
    """EWMA of realised/predicted days for forecasts that were at least partly right.

    A ratio above 1 means the desk is too optimistic about speed, so the scale
    grows and future date estimates move out; a ratio below 1 pulls them in.
    """
    try:
        current = float(database.get_setting("forecast_time_scale", "1.0") or 1.0)
    except (TypeError, ValueError):
        current = 1.0
    ratios = []
    for block in (calibration.get("by_term") or {}).values():
        if block.get("days_ratio"):
            ratios.append(float(block["days_ratio"]))
    if not ratios or calibration.get("n", 0) < 3:
        return max(0.55, min(2.20, current))
    observed = sum(ratios) / len(ratios)
    updated = 0.75 * current + 0.25 * observed
    return round(max(0.55, min(2.20, updated)), 4)


def confidence_adjustment(calibration: dict[str, Any], term: str, confidence: float) -> float:
    """Multiplier applied to a raw confidence before it is stored.

    Cold start (fewer than 5 grades) changes nothing. Once there is a record,
    a term that hits its targets gets a boost and a term that misses gets
    shaded down; confidence buckets are adjusted the same way so a model that
    is only accurate at high confidence stops issuing confident low-bucket calls.
    """
    n = int(calibration.get("n") or 0)
    if n < 5:
        return 1.0
    block = (calibration.get("by_term") or {}).get(term.upper()) or calibration
    hit = float(block.get("hit_rate") or calibration.get("hit_rate") or 0.5)
    factor = 0.85 + 0.35 * max(0.0, min(1.0, hit))
    bucket = "high" if confidence >= 0.70 else "mid" if confidence >= 0.50 else "low"
    bucket_block = (calibration.get("by_bucket") or {}).get(bucket)
    if bucket_block:
        bucket_hit = float(bucket_block.get("hit_rate") or 0.5)
        if bucket_hit < 0.35:
            factor *= 0.90
        elif bucket_hit > 0.65:
            factor *= 1.05
    return round(max(0.60, min(1.25, factor)), 3)
