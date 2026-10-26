"""Follow-up consultant chat on top of the last analysis.

The Bull/Bear/Arbitrator debate produces a book and a market view, but the
reasoning behind an individual call is often worth interrogating. This module
answers free-form questions using only the persisted scan snapshot plus the
live open book, so it never invents prices or re-runs the pipeline.
"""

from __future__ import annotations

import json

import config
import database
from analytics import size_book
from intelligence import client

SYSTEM = """You are the quantitative desk consultant for a Hypixel SkyBlock Bazaar \
trading engine. You are speaking to the trader who owns the dashboard, after the \
Bull/Bear/Arbitrator analysis has already run.

How you work:
- Answer only from the SCAN CONTEXT and POSITIONS blocks supplied below. If the data \
does not contain the answer (e.g. an item was not a candidate, or no price history \
exists), say so plainly and state what data would be needed. Never invent prices, \
volumes or news.
- You may reason from the data that is present: price vs MA, dip_z (sigma below the \
item's own recent mean), confluence (cross-source agreement), risk tier, spread, \
liquidity, catalysts, bull/bear cases, and the track record.
- The desk's BUY rule is: price below the buy threshold vs its moving average, and \
for same-day baselines at least 0.5 sigma below its own volatility. HOLD is a watch \
note, not a rejection.
- When asked how much to buy, use the units and capital already computed in \
POSITIONS, or compute units = floor(capital / entry_price) if the item is not there. \
Flag when a position would exceed the per-slot cap.
- purse_coins, capital_committed and cash_left are pre-computed in the context. \
Quote them as-is; never re-derive the purse or report a different magnitude.
- Confidence is a 0-1 score; anything under 0.55 is a low-conviction, small-size call.
- Be direct and concise: 2-6 short paragraphs or a small table. Use markdown. No \
disclaimers about financial advice beyond a one-line risk note when a stop is close.
- If the user asks about something outside SkyBlock investing, say it is out of scope."""


def remembered_purse() -> float:
    """Purse saved by the sidebar, so the desk remembers it across restarts."""
    try:
        return float(
            database.get_setting("last_purse", str(config.DEFAULT_PURSE_COINS))
            or config.DEFAULT_PURSE_COINS
        )
    except (TypeError, ValueError):
        return float(config.DEFAULT_PURSE_COINS)


def _scan_snapshot() -> dict:
    raw = database.get_setting("last_analysis_context", "") or ""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def _positions(purse: float) -> list[dict]:
    """Live open book, enriched with the same unit sizing as the screener."""
    open_signals = database.list_signals(status="open", limit=100)
    if purse > 0 and open_signals:
        stats = database.performance_stats(days=30)
        multiplier = 1.0 + (stats["win_rate"] - 0.5) * 0.6
        sizes = {
            row["item_id"]: row
            for row in size_book(open_signals, purse, accuracy_multiplier=multiplier)
        }
    else:
        sizes = {}
    positions = []
    for s in open_signals:
        sized = sizes.get(s["item_id"]) or {}
        positions.append(
            {
                "item_id": s["item_id"],
                "signal": s["signal"],
                "confidence": round(float(s.get("confidence") or 0), 2),
                "entry_price": s.get("entry_price"),
                "target_price": s.get("target_price"),
                "stop_price": s.get("stop_price"),
                "risk_ratio": s.get("risk_ratio"),
                "risk_tier": s.get("risk_tier"),
                "opened_at": s.get("created_at"),
                "units": sized.get("units"),
                "capital": sized.get("capital"),
                "target_profit": sized.get("target_profit"),
                "stop_risk": sized.get("stop_risk"),
            }
        )
    return positions


def build_context(purse: float = 0.0) -> dict:
    snapshot = _scan_snapshot()
    positions = _positions(purse)
    committed = sum(
        float(p.get("capital") or 0)
        for p in positions
        if (p.get("signal") or "").upper() == "BUY"
    )
    return {
        "purse_coins": round(float(purse), 0),
        "capital_committed": round(committed),
        "cash_left": round(max(float(purse) - committed, 0.0)),
        "scanned_at": snapshot.get("scanned_at"),
        "threshold": snapshot.get("threshold"),
        "track_record": snapshot.get("track_record") or database.performance_stats(days=30),
        "market_view": snapshot.get("market_view"),
        "bull_overview": snapshot.get("bull_overview"),
        "bear_overview": snapshot.get("bear_overview"),
        "signals_from_scan": snapshot.get("signals"),
        "candidates": snapshot.get("candidates"),
        "positions": positions,
    }


def context_summary(purse: float = 0.0) -> dict:
    """Small header used by the UI (no model call)."""
    snapshot = _scan_snapshot()
    purse = float(purse or 0) or remembered_purse()
    return {
        "has_snapshot": bool(snapshot),
        "scanned_at": snapshot.get("scanned_at") or "",
        "market_view": snapshot.get("market_view") or "",
        "candidates": len(snapshot.get("candidates") or []),
        "open_signals": len(database.list_signals(status="open", limit=100)),
        "threshold": snapshot.get("threshold") or {},
        "purse": purse,
    }


def _coins(value) -> str:
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return "-"


def _mentioned_items(question: str, context: dict) -> list[dict]:
    """Find book rows whose item name appears in the question."""
    text = question.lower().replace("_", " ")
    hits = []
    for row in (context.get("positions") or []):
        name = str(row.get("item_id") or "").replace("_", " ").lower()
        if name and (name in text or str(row.get("item_id") or "").lower() in text):
            hits.append(row)
    return hits


def _candidate(context: dict, item_id: str) -> dict:
    for row in context.get("candidates") or []:
        if row.get("item_id") == item_id:
            return row
    return {}


def _offline_answer(question: str, context: dict) -> str:
    """Deterministic answer when the model endpoint is blocked.

    The dashboard still has to be useful during a Cloudflare outage, so the
    common questions (sizing, why-HOLD, when-to-sell) are answered straight
    from the stored book.
    """
    lowered = question.lower()
    positions = context.get("positions") or []

    sizing_words = ("how many", "unit", "size", "sizing", "capital", "allocate",
                    "allocation", "how much", "spend", "invest")
    sell_words = ("sell", "exit", "take profit", "target", "stop")
    hold_words = ("hold", "watch", "not buy", "waiting", "on hold", "on watch")
    mentioned = _mentioned_items(question, context)
    mentioned_holds = [
        row for row in mentioned if (row.get("signal") or "").upper() == "HOLD"
    ]

    if positions and any(word in lowered for word in sizing_words):
        rows = [p for p in positions if (p.get("signal") or "").upper() == "BUY"]
        lines = [
            f"**Sizing against {_coins(context.get('purse_coins'))} coins purse** "
            "(Balanced profile: 15% per slot, 4 slots, 10% cash reserve).",
            "",
            "| Item | Units | Capital | Target | Stop | Target profit |",
            "|---|---|---|---|---|---|",
        ]
        committed = 0.0
        for row in rows:
            units = row.get("units")
            capital = float(row.get("capital") or 0)
            committed += capital
            lines.append(
                f"| {row['item_id']} | {units if units is not None else '-'} | "
                f"{_coins(capital)} | {_coins(row.get('target_price'))} | "
                f"{_coins(row.get('stop_price'))} | {_coins(row.get('target_profit'))} |"
            )
        purse = float(context.get("purse_coins") or 0)
        lines += [
            "",
            f"Total committed **{_coins(committed)}** · deployable left "
            f"**{_coins(max(purse - committed, 0))}**.",
            "",
            "_Model endpoint is temporarily blocked (Cloudflare), so this answer is "
            "computed directly from the stored book, not generated._",
        ]
        return "\n".join(lines)

    if positions and any(word in lowered for word in sell_words):
        lines = [
            "**Exit levels for the open book**",
            "",
            "| Item | Signal | Entry | Target | Stop |",
            "|---|---|---|---|---|",
        ]
        for row in positions:
            lines.append(
                f"| {row['item_id']} | {row['signal']} | {_coins(row.get('entry_price'))} | "
                f"{_coins(row.get('target_price'))} | {_coins(row.get('stop_price'))} |"
            )
        lines += [
            "",
            "A BUY closes on target, stop, or the 14-day horizon; a SELL closes when "
            "price reclaims entry. Discord gets an exit alert on every close.",
            "",
            "_Model endpoint is temporarily blocked (Cloudflare); levels come straight "
            "from the database._",
        ]
        return "\n".join(lines)

    if positions and (mentioned_holds or any(word in lowered for word in hold_words)):
        holds = [p for p in positions if p.get("signal") == "HOLD"]
        hold_rows = mentioned_holds or sorted(
            holds,
            key=lambda p: float(
                _candidate(context, p["item_id"]).get("risk_ratio")
                or p.get("risk_ratio")
                or 9.9
            ),
        )[:3]
        if hold_rows:
            lines = ["**Why these positions are on watch, not BUY**", ""]
            for row in hold_rows:
                cand = _candidate(context, row["item_id"])
                ratio = cand.get("risk_ratio") or row.get("risk_ratio")
                dip_z = cand.get("dip_z")
                try:
                    ratio_text = f"{float(ratio):.4f}x"
                    dip_text = f"{float(dip_z):+.2f}σ"
                    passes = float(ratio) <= 0.985 and float(dip_z) <= -0.5
                except (TypeError, ValueError):
                    ratio_text = "-"
                    dip_text = "n/a"
                    passes = False
                reason = (
                    "clears the quant gate, so the HOLD came from the debate (fatal bear "
                    "case or no evidence coverage) — treat it as a size-down candidate."
                    if passes
                    else "does not clear both gates (needs ≤0.985x and ≤−0.5σ)."
                )
                lines.append(
                    f"- **{row['item_id']}** — price/MA {ratio_text}, dip {dip_text}, "
                    f"tier {row.get('risk_tier') or '-'}, confluence "
                    f"{cand.get('confluence_score') if cand.get('confluence_score') is not None else '-'}. "
                    f"It {reason}"
                )
            lines += [
                "",
                "_Model endpoint is temporarily blocked (Cloudflare); this is the "
                "quant gate read-out, not the AI debate._",
            ]
            return "\n".join(lines)

    named = [row for row in mentioned if (row.get("signal") or "").upper() != "HOLD"]
    if named:
        theses = {
            row.get("item_id"): row.get("thesis")
            for row in (context.get("signals_from_scan") or [])
        }
        lines = ["**Read on the named position**", ""]
        for row in named[:3]:
            cand = _candidate(context, row["item_id"])
            try:
                ratio_text = f"{float(cand.get('risk_ratio')):.4f}x"
            except (TypeError, ValueError):
                ratio_text = "-"
            try:
                dip_text = f"{float(cand.get('dip_z')):+.2f}σ"
            except (TypeError, ValueError):
                dip_text = "n/a"
            lines += [
                f"**{row['item_id']}** · {row['signal']} · confidence "
                f"{row.get('confidence')} · tier {row.get('risk_tier') or '-'}",
                "",
                f"- Entry {_coins(row.get('entry_price'))} · target "
                f"{_coins(row.get('target_price'))} · stop {_coins(row.get('stop_price'))}",
                f"- Price/MA7 {ratio_text} · dip {dip_text} · confluence "
                f"{cand.get('confluence_score') if cand.get('confluence_score') is not None else '-'}",
                f"- Thesis: {(theses.get(row['item_id']) or 'no thesis recorded')[:500]}",
                "",
            ]
        lines.append(
            "_Model endpoint is temporarily blocked (Cloudflare); this is the stored "
            "analysis, not a new opinion._"
        )
        return "\n".join(lines)

    buys = sorted(
        [p for p in positions if (p.get("signal") or "").upper() == "BUY"],
        key=lambda p: float(p.get("confidence") or 0),
        reverse=True,
    )
    lines = [
        f"**Book snapshot** — {len(positions)} open calls, {len(buys)} BUY.",
        "",
    ]
    if context.get("market_view"):
        lines += [f"Last market view: {context['market_view']}", ""]
    for row in buys[:6]:
        lines.append(
            f"- **{row['item_id']}** · entry {_coins(row.get('entry_price'))} · "
            f"target {_coins(row.get('target_price'))} · stop {_coins(row.get('stop_price'))} · "
            f"confidence {row.get('confidence')}"
        )
    lines += [
        "",
        "_The model endpoint is temporarily blocked (Cloudflare), so this is a stored "
        "summary. Try the question again in a few minutes._",
    ]
    return "\n".join(lines)


def _transcript(history: list[dict] | None) -> str:
    if not history:
        return ""
    lines = []
    for message in history[-8:]:
        role = "Trader" if message.get("role") == "user" else "Consultant"
        content = str(message.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content[:700]}")
    return "\n".join(lines)


def ask(
    question: str,
    history: list[dict] | None = None,
    purse: float = 0.0,
) -> str:
    """Answer one follow-up question from the persisted analysis."""
    question = (question or "").strip()
    if not question:
        raise client.IntelligenceError("Ask a question first.")
    if len(question) > 2000:
        question = question[:2000]

    purse = float(purse or 0) or remembered_purse()
    context = build_context(purse)
    if not context.get("scanned_at") and not context.get("positions"):
        raise client.IntelligenceError(
            "No analysis on record yet — run a scan first, then ask."
        )

    blocks = [
        "SCAN CONTEXT (authoritative, do not contradict):",
        json.dumps(context, ensure_ascii=False, default=str),
    ]
    previous = _transcript(history)
    if previous:
        blocks.append("\nCONVERSATION SO FAR:\n" + previous)
    blocks.append("\nQUESTION:\n" + question)
    user = "\n".join(blocks)

    # Keep the prompt bounded: Cerebras handles context well, but the snapshot
    # already trims itself, and a runaway transcript should not double the cost.
    if len(user) > 14000:
        user = user[:14000] + "\n[truncated]"

    try:
        return client.chat_text(
            SYSTEM,
            user,
            purpose="consultant",
            temperature=0.35,
            max_tokens=1200,
        )
    except client.IntelligenceError:
        # The sandbox endpoint is intermittently Cloudflare-blocked. Rather than
        # leaving the trader with an error, answer the common questions from the
        # stored book deterministically.
        return _offline_answer(question, context)
