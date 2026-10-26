"""Hypixel Bazaar feed: spot prices, order-book depth and weekly volume."""

from __future__ import annotations

import requests

import config


class BazaarError(RuntimeError):
    pass


def fetch_bazaar(timeout: int = 25) -> dict:
    headers = {"User-Agent": "skyblock-ai-dashboard/1.0"}
    if config.HYPIXEL_API_KEY:
        headers["API-Key"] = config.HYPIXEL_API_KEY
    try:
        resp = requests.get(config.HYPIXEL_BAZAAR_URL, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise BazaarError(f"Hypixel request failed: {exc}") from exc
    if resp.status_code != 200:
        raise BazaarError(f"Hypixel returned HTTP {resp.status_code}")
    payload = resp.json()
    if not payload.get("success", True):
        raise BazaarError(f"Hypixel error: {payload.get('cause', 'unknown')}")
    return payload


def display_name(item_id: str) -> str:
    return item_id.replace("_", " ").title()


def normalize_products(payload: dict) -> list[dict]:
    """Flatten the raw bazaar payload into one row per product.

    buy_price  = highest standing buy order (what you receive selling instantly)
    sell_price = lowest standing sell offer (what you pay buying instantly)
    """
    products = payload.get("products") or {}
    rows: list[dict] = []
    for item_id, product in products.items():
        quick = product.get("quick_status") or {}
        buy = float(quick.get("buyPrice") or 0)
        sell = float(quick.get("sellPrice") or 0)
        if buy <= 0 and sell <= 0:
            continue
        if buy <= 0:
            buy = sell
        if sell <= 0:
            sell = buy
        mid = (buy + sell) / 2
        buy_week = float(quick.get("buyMovingWeek") or 0)
        sell_week = float(quick.get("sellMovingWeek") or 0)
        rows.append(
            {
                "item_id": item_id,
                "item_name": display_name(item_id),
                "buy_price": buy,
                "sell_price": sell,
                "mid_price": mid,
                "buy_volume": float(quick.get("buyVolume") or 0),
                "sell_volume": float(quick.get("sellVolume") or 0),
                "weekly_volume_items": buy_week + sell_week,
                "weekly_volume_coins": (buy_week + sell_week) * mid,
                "buy_orders": len(product.get("buy_summary") or []),
                "sell_orders": len(product.get("sell_summary") or []),
                "spread_pct": ((sell - buy) / mid * 100) if mid else 0.0,
            }
        )
    return rows
