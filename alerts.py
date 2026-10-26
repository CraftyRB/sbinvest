"""Optional Discord webhook push alerts for new signals and exits."""

from __future__ import annotations

import json

import requests

import config
import database

# Why a position was closed, mapped to a readable heading and embed colour.
EXIT_REASONS = {
    "target": ("Target hit - ready to sell", 0x2ECC71),
    "stop": ("Stop hit - exit now", 0xE74C3C),
    "thesis_invalidated": ("Thesis invalidated - close", 0xF39C12),
    "horizon": ("Horizon reached - close", 0x95A5A6),
}

# Portfolio cash-out tiers, checked against each holding's unrealised P&L.
PORTFOLIO_TIERS = {
    "strong": ("Strong take-profit - sell the bulk", 0x1ABC9C),
    "target": ("Take-profit reached - cash out", 0x2ECC71),
    "stop": ("Stop-loss hit - exit now", 0xE74C3C),
}
PORTFOLIO_STATE_KEY = "portfolio_alert_state"


def _post(url: str, payload: dict) -> bool:
    try:
        resp = requests.post(url, json=payload, timeout=15)
        return resp.status_code in (200, 204)
    except requests.RequestException:
        return False


def push_signals(signals: list[dict], market_view: str = "", source: str = "ai") -> bool:
    if not config.DISCORD_WEBHOOK_URL:
        return False

    from intelligence import budget

    lines = []
    for s in signals[:10]:
        term = s.get("term") or "SHORT"
        eta = s.get("expected_target_date") or "-"
        lines.append(
            f"**{s['item_id']}** · `{s['signal']}` · {term} @ {s['entry_price']:,.0f} coins "
            f"(target {s['target_price']:,.0f} by {eta}) | conf {s['confidence']:.0%}"
        )
    description = "\n".join(lines) or "No actionable signals this scan."
    embed = {
        "title": "SkyBlock AI Investment Desk - new scan" ,
        "description": f"{market_view}\n\n{description}"[:4000],
        "color": 0x2F81F7,
        "footer": {"text": f"source: {source} | model: {budget.model()}"},
    }
    return _post(config.DISCORD_WEBHOOK_URL, {"embeds": [embed]})


def build_exit_embed(
    signal: dict, resolved: dict, exit_price: float, reason: str
) -> dict:
    """Compose the 'time to sell' embed so it can be unit-tested offline."""
    title, color = EXIT_REASONS.get(reason, ("Position closed", 0x95A5A6))
    entry = float(signal.get("entry_price") or 0)
    pnl_pct = float(resolved.get("pnl_pct") or 0.0)
    status = resolved.get("status") or "-"
    target = signal.get("target_price")
    stop = signal.get("stop_price")
    detail = (
        f"`{signal.get('signal', '')}` entered at **{entry:,.0f}** coins, "
        f"closed at **{float(exit_price):,.0f}** -> **{pnl_pct:+.2f}%** ({status})."
    )
    fields = []
    if target:
        fields.append({"name": "Target", "value": f"{float(target):,.0f}", "inline": True})
    if stop:
        fields.append({"name": "Stop", "value": f"{float(stop):,.0f}", "inline": True})
    return {
        "title": f"{title}: {signal.get('item_id', '')}",
        "description": detail,
        "color": color,
        "fields": fields,
        "footer": {"text": "SkyBlock AI Investment Desk - exit alert"},
    }


def push_exit(
    signal: dict, resolved: dict, exit_price: float, reason: str = "horizon"
) -> bool:
    """Notify Discord when an open position hits its target, stop or horizon.

    This fires whenever a webhook is configured, independently of the new-scan
    alert toggle: missing a take-profit is worse than an extra message.
    """
    if not config.DISCORD_WEBHOOK_URL:
        return False
    return _post(
        config.DISCORD_WEBHOOK_URL,
        {"embeds": [build_exit_embed(signal, resolved, exit_price, reason)]},
    )


# --------------------------------------------------------------------------- #
# Portfolio cash-out alerts (separate webhook, driven by the user's holdings)
# --------------------------------------------------------------------------- #

def portfolio_tier(pnl_pct: float) -> str | None:
    """Which cash-out rule, if any, this unrealised P&L has crossed."""
    if pnl_pct <= -config.PORTFOLIO_STOP_LOSS_PCT * 100:
        return "stop"
    if pnl_pct >= config.PORTFOLIO_STRONG_TAKE_PROFIT_PCT * 100:
        return "strong"
    if pnl_pct >= config.PORTFOLIO_TAKE_PROFIT_PCT * 100:
        return "target"
    return None


def build_portfolio_embed(row: dict, tier: str, portfolio_name: str) -> dict:
    """Compose a cash-out alert for one holding, sellable at the current bid."""
    heading, color = PORTFOLIO_TIERS[tier]
    qty = float(row.get("qty") or 0)
    cost_basis = float(row.get("cost_basis") or 0)
    market = float(row.get("market_price") or 0)
    pnl = float(row.get("pnl") or 0)
    pnl_pct = float(row.get("pnl_pct") or 0)
    value = float(row.get("value") or 0)
    rule = {
        "strong": f"+{config.PORTFOLIO_STRONG_TAKE_PROFIT_PCT:.0%} take-profit",
        "target": f"+{config.PORTFOLIO_TAKE_PROFIT_PCT:.0%} take-profit",
        "stop": f"-{config.PORTFOLIO_STOP_LOSS_PCT:.0%} stop-loss",
    }[tier]
    return {
        "title": f"{heading}: {row.get('item_id', '')}",
        "description": (
            f"Holding **{qty:,.0f}** units at cost **{cost_basis:,.0f}** each. "
            f"Top bid now **{market:,.0f}** -> **{pnl_pct:+.1f}%**.\n"
            f"Cash out **{value:,.0f}** coins at the current bid "
            f"(**{pnl:+,.0f}** vs cost)."
        ),
        "color": color,
        "fields": [
            {"name": "Trigger", "value": rule, "inline": True},
            {"name": "Realised if sold", "value": f"{pnl:+,.0f} coins", "inline": True},
            {"name": "Sector", "value": row.get("sector") or "OTHER", "inline": True},
        ],
        "footer": {"text": f"Portfolio: {portfolio_name} | cash-out alert"},
    }


def push_portfolio_exits(rows: list[dict], portfolio_name: str) -> dict:
    """Send cash-out alerts for holdings that crossed a take-profit or stop tier.

    Alerts are edge-triggered: a tier fires once when crossed and re-arms only
    after the holding drops back below it, so a 30-minute scan loop does not
    repeat the same message.
    """
    result = {"sent": 0, "triggered": [], "configured": bool(config.DISCORD_PORTFOLIO_WEBHOOK_URL)}
    if not config.DISCORD_PORTFOLIO_WEBHOOK_URL or not rows:
        return result

    try:
        state = json.loads(database.get_setting(PORTFOLIO_STATE_KEY) or "{}")
    except ValueError:
        state = {}

    for row in rows:
        key = str(row.get("id"))
        tier = portfolio_tier(float(row.get("pnl_pct") or 0))
        previous = state.get(key)
        if tier is None:
            state.pop(key, None)
            continue
        if tier == previous:
            continue
        if _post(
            config.DISCORD_PORTFOLIO_WEBHOOK_URL,
            {"embeds": [build_portfolio_embed(row, tier, portfolio_name)]},
        ):
            state[key] = tier
            result["sent"] += 1
            result["triggered"].append(f"{row.get('item_id')}:{tier}")

    database.set_setting(PORTFOLIO_STATE_KEY, json.dumps(state))
    return result


def build_exit_order_embed(order: dict, bid: float, portfolio_name: str) -> dict:
    """Compose the alert for a user-placed 'sell N at price X' order."""
    qty = float(order.get("qty") or 0)
    target = float(order.get("target_price") or 0)
    proceeds = qty * bid
    return {
        "title": f"Limit exit hit: {order.get('item_id')}",
        "description": (
            f"Your plan was to sell **{qty:,.0f}** units at **{target:,.0f}**. "
            f"The top bid is now **{bid:,.0f}**.\n"
            f"Sell those {qty:,.0f} units for **{proceeds:,.0f}** coins."
        ),
        "color": 0x2ECC71,
        "fields": [
            {"name": "Target", "value": f"{target:,.0f}", "inline": True},
            {"name": "Qty", "value": f"{qty:,.0f}", "inline": True},
            {"name": "Proceeds", "value": f"{proceeds:,.0f} coins", "inline": True},
        ],
        "footer": {"text": f"Portfolio: {portfolio_name} | limit exit order"},
    }


def push_exit_order_hits(
    orders: list[dict], prices: dict[str, dict], portfolio_name: str
) -> dict:
    """Alert once when the bid reaches a holding's chosen partial-exit price."""
    result = {"sent": 0, "hit": [], "configured": bool(config.DISCORD_PORTFOLIO_WEBHOOK_URL)}
    if not config.DISCORD_PORTFOLIO_WEBHOOK_URL or not orders:
        return result

    for order in orders:
        market = prices.get(order.get("item_id")) or {}
        bid = float(market.get("buy_price") or 0)
        target = float(order.get("target_price") or 0)
        if bid <= 0 or target <= 0 or bid < target:
            continue
        if _post(
            config.DISCORD_PORTFOLIO_WEBHOOK_URL,
            {"embeds": [build_exit_order_embed(order, bid, portfolio_name)]},
        ):
            database.mark_exit_order_hit(int(order["id"]), bid)
            result["sent"] += 1
            result["hit"].append(f"{order.get('item_id')}@{target:,.0f}")

    return result

