"""Portfolio maths: position sizing, holdings valuation and sector allocation."""

from __future__ import annotations

import csv
import io
from typing import Any


SECTOR_KEYWORDS = {
    "FARMING": (
        "WHEAT", "CARROT", "POTATO", "PUMPKIN", "MELON", "SUGAR", "CACTUS",
        "MUSHROOM", "COCOA", "NETHER_WART", "HAY", "BALE", "SEEDS", "FARMING",
        "ENCHANTED_BREAD", "COMPOST",
    ),
    "MINING": (
        "MITHRIL", "TITANIUM", "GEMSTONE", "GLACITE", "DWARVEN", "COBBLESTONE",
        "COAL", "IRON", "GOLD", "DIAMOND", "EMERALD", "LAPIS", "REDSTONE",
        "QUARTZ", "OBSIDIAN", "NETHERRACK", "SULFUR", "UMBER", "TUNGSTEN",
        "AMBER", "JADE", "SAPPHIRE", "TOPAZ", "RUBY", "JASPER", "OPAL",
    ),
    "COMBAT": (
        "DRAGON", "WITHER", "ROTTEN", "BONE", "STRING", "SPIDER", "BLAZE",
        "ENDER", "SLAYER", "FLESH", "GHAST", "MAGMA", "NETHER_STAR", "SUMMONING",
        "SHARD", "KATANA", "SWORD", "BOW", "ARMOR",
    ),
    "FISHING": ("FISH", "SALMON", "COD", "PUFFER", "CLOWNFISH", "SPONGE", "PRISMARINE", "FISHING"),
    "FORAGING": ("LOG", "WOOD", "LEAF", "SAPLING", "FORAGING", "OAK", "BIRCH", "SPRUCE", "JUNGLE"),
    "ALCHEMY": ("POTION", "BREWING", "ALCHEMY", "ENCHANTED_BOOK", "ENCHANT", "SOUL", "ESSENCE"),
}


def sector_for(item_id: str) -> str:
    upper = item_id.upper()
    for sector, keywords in SECTOR_KEYWORDS.items():
        if any(k in upper for k in keywords):
            return sector
    return "OTHER"


def allocation_plan(
    purse: float,
    max_position_pct: float = 0.15,
    reserve_pct: float = 0.10,
    active_slots: int = 4,
    accuracy_multiplier: float = 1.0,
) -> dict[str, Any]:
    """Max coins per trade given purse, risk cap, cash reserve and model accuracy."""
    purse = max(float(purse), 0.0)
    multiplier = max(0.5, min(1.3, float(accuracy_multiplier)))
    deployable = purse * (1 - reserve_pct)
    raw_position = deployable * max_position_pct * multiplier
    slot_ceiling = deployable / active_slots if active_slots > 0 else 0.0
    per_trade = min(raw_position, slot_ceiling) if active_slots > 0 else 0.0
    total_deployed = min(per_trade * active_slots, deployable)
    return {
        "purse": round(purse, 2),
        "reserve": round(purse * reserve_pct, 2),
        "deployable": round(deployable, 2),
        "max_position_pct": max_position_pct,
        "accuracy_multiplier": round(multiplier, 2),
        "max_per_trade": round(per_trade, 2),
        "active_slots": active_slots,
        "total_deployed": round(total_deployed, 2),
        "cash_left": round(purse - total_deployed, 2),
    }


def size_book(
    signals: list[dict],
    purse: float,
    max_position_pct: float = 0.15,
    reserve_pct: float = 0.10,
    active_slots: int = 4,
    accuracy_multiplier: float = 1.0,
) -> list[dict]:
    """Turn open BUY calls into order sizes that fit the purse.

    Highest-confidence calls are funded first, and every trade is capped by both
    the per-slot ceiling and the cash still unallocated, so the whole book never
    commits more than `allocation_plan` allows.
    """
    plan = allocation_plan(
        purse, max_position_pct, reserve_pct, active_slots, accuracy_multiplier
    )
    remaining = plan["deployable"]
    sized: list[dict] = []
    buyable = [
        s
        for s in signals
        if (s.get("signal") or "").upper() == "BUY"
        and (s.get("status") or "open") == "open"
        and float(s.get("entry_price") or 0) > 0
    ]
    for signal in sorted(buyable, key=lambda s: float(s.get("confidence") or 0), reverse=True):
        entry = float(signal["entry_price"])
        slot_cash = min(plan["max_per_trade"], remaining)
        qty = int(slot_cash // entry)
        if qty <= 0:
            continue
        capital = qty * entry
        remaining -= capital
        target = float(signal.get("target_price") or 0)
        stop = float(signal.get("stop_price") or 0)
        sized.append(
            {
                "item_id": signal["item_id"],
                "term": (signal.get("term") or "SHORT"),
                "signal": signal["signal"],
                "confidence": float(signal.get("confidence") or 0),
                "entry": round(entry, 2),
                "units": qty,
                "capital": round(capital, 2),
                "target_profit": round(qty * max(target - entry, 0), 2),
                "stop_risk": round(qty * max(entry - stop, 0), 2),
                "cash_after": round(remaining, 2),
            }
        )
    return sized


def value_holdings(holdings: list[dict], prices: dict[str, dict]) -> list[dict]:
    """Mark holdings to the latest bazaar snapshot."""
    rows = []
    for h in holdings:
        market = prices.get(h["item_id"])
        # Liquidate at the top bid; fall back to cost basis when no snapshot yet.
        exit_price = (market or {}).get("buy_price") or h["cost_basis"]
        cost = h["qty"] * h["cost_basis"]
        value = h["qty"] * exit_price
        pnl = value - cost
        rows.append(
            {
                **h,
                "sector": sector_for(h["item_id"]),
                "market_price": round(exit_price, 2),
                "cost": round(cost, 2),
                "value": round(value, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round((pnl / cost * 100) if cost else 0.0, 2),
                "as_of": (market or {}).get("ts", "cost basis"),
            }
        )
    return rows


def portfolio_summary(rows: list[dict], purse: float) -> dict[str, Any]:
    cost = sum(r["cost"] for r in rows)
    value = sum(r["value"] for r in rows)
    sectors: dict[str, float] = {}
    for r in rows:
        sectors[r["sector"]] = sectors.get(r["sector"], 0.0) + r["value"]
    allocation = {
        s: round(v / value * 100, 2) if value else 0.0 for s, v in sorted(sectors.items())
    }
    return {
        "purse": round(purse, 2),
        "invested_cost": round(cost, 2),
        "market_value": round(value, 2),
        "unrealized_pnl": round(value - cost, 2),
        "unrealized_pnl_pct": round(((value - cost) / cost * 100) if cost else 0.0, 2),
        "cash": round(purse - cost, 2),
        "sector_allocation": allocation,
    }


def statement_csv(rows: list[dict], summary: dict[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["item_id", "sector", "qty", "cost_basis", "market_price", "cost", "value", "pnl", "pnl_pct"])
    for r in rows:
        writer.writerow(
            [
                r["item_id"], r["sector"], r["qty"], r["cost_basis"], r["market_price"],
                r["cost"], r["value"], r["pnl"], r["pnl_pct"],
            ]
        )
    writer.writerow([])
    writer.writerow(["summary"])
    for key, value in summary.items():
        if key != "sector_allocation":
            writer.writerow([key, value])
    writer.writerow([])
    writer.writerow(["sector_allocation"])
    for sector, pct in summary.get("sector_allocation", {}).items():
        writer.writerow([sector, pct])
    return buffer.getvalue()
