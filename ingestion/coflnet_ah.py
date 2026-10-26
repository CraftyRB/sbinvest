"""Coflnet Auction House API: LBIN price statistics and item metadata.

Working endpoints (verified):
  GET /api/items              -> item metadata (name, tag, flags)
  GET /api/item/price/{TAG}   -> min/median/mean/mode/max/volume for recent LBINs

There is no public historical series, so each poll is persisted in SQLite and
`database.lbin_history()` builds the local history over time.
"""

from __future__ import annotations

import requests

import config
import database


class CoflnetError(RuntimeError):
    pass


def _get(path: str, timeout: int | None = None):
    url = f"{config.COFLNET_API_URL}{path}"
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "skyblock-ai-dashboard/1.0", "Accept": "application/json"},
            timeout=timeout or config.SOURCE_REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise CoflnetError(f"Coflnet request failed: {exc}") from exc
    if resp.status_code != 200:
        raise CoflnetError(f"Coflnet HTTP {resp.status_code} for {path}")
    return resp.json()


def fetch_item_metadata() -> dict[str, str]:
    """Return tag -> flags ('BAZAAR', 'AUCTION', ...)."""
    data = _get("/items", timeout=40)
    if not isinstance(data, list):
        raise CoflnetError("Unexpected /items payload")
    return {str(row.get("tag", "")).upper(): str(row.get("flags", "")) for row in data}


def fetch_lbin(tag: str) -> dict | None:
    try:
        data = _get(f"/item/price/{tag.upper()}")
    except CoflnetError:
        return None
    if not isinstance(data, dict) or data.get("median") is None:
        return None
    return {
        "tag": tag.upper(),
        "min": data.get("min"),
        "median": data.get("median"),
        "mean": data.get("mean"),
        "mode": data.get("mode"),
        "max": data.get("max"),
        "volume": data.get("volume"),
    }


def collect_ah_intel(tags: list[str], limit: int = 6) -> dict:
    """Fetch LBIN stats for the requested tags (deduped, capped) and persist."""
    items: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()
    flags: dict[str, str] = {}

    try:
        flags = fetch_item_metadata()
    except Exception as exc:
        errors.append(f"metadata: {exc}")

    for raw_tag in tags:
        tag = str(raw_tag or "").upper().strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        row = fetch_lbin(tag)
        if row:
            row["flags"] = flags.get(tag, "")
            items.append(row)
        if len(items) >= limit:
            break

    if items:
        database.save_auction_stats(items)
    return {"items": items, "errors": errors}
