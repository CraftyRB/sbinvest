"""Live Signal Screener tab."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import database
from analytics import size_book

SIGNAL_ORDER = {"BUY": 0, "SELL": 1, "HOLD": 2}


def _fmt_coins(value: float | None) -> str:
    if value is None:
        return "-"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,.0f}"


def render(last_scan: dict | None, purse: float = 0.0) -> None:
    st.subheader("Live Signal Screener")
    st.caption(
        "AI-flagged Bazaar opportunities. Entry uses the instant-buy ask; target and stop "
        "come from the investment thesis."
    )
    st.caption(
        "BUY rule: price must sit below the buy threshold (price/MA) and, for same-day "
        "baselines, at least 0.5σ below that item's own recent volatility. HOLD is a watch "
        "note — it means the dip is not deep enough yet, not that the desk is out of ideas."
    )

    if last_scan:
        if last_scan.get("error"):
            st.error(f"Last scan failed: {last_scan['error']}")
        else:
            source = last_scan.get("source", "-")
            st.caption(
                f"Last scan {last_scan.get('scanned_at', '')} · source **{source}** · "
                f"model `{last_scan.get('model', '')}` · {last_scan.get('products', 0):,} products · "
                f"{last_scan.get('stored', 0)} new signals"
            )
            if last_scan.get("market_view"):
                st.info(last_scan["market_view"])
            filt = last_scan.get("evidence_filter") or {}
            if filt:
                pool = int(filt.get("pool_total") or (int(filt.get("kept", 0)) + int(filt.get("dropped", 0))))
                accepted = int(filt.get("kept") or 0)
                acceptance = float(filt.get("acceptance_pct") or 0)
                targeted = last_scan.get("active_search") or {}
                added = sum(
                    int(targeted.get(k) or 0) for k in ("reddit", "youtube", "wiki", "web")
                )
                caption = (
                    f"Evidence gate: kept **{accepted}** of **{pool}** plausible items "
                    f"(**{acceptance:.0%}** accepted, {int(filt.get('prefiltered_out') or 0)} "
                    f"off-topic dropped before audit)"
                )
                if filt.get("targeted_auto"):
                    caption += (
                        f" · {int(filt['targeted_auto'])} kept as item-specific hits from the "
                        f"planned searches"
                    )
                long_filt = last_scan.get("evidence_filter_long") or {}
                if long_filt:
                    caption += (
                        f" · long pass kept **{int(long_filt.get('kept') or 0)}** of "
                        f"**{int(long_filt.get('pool_total') or 0)}**"
                    )
                if added:
                    caption += (
                        f" · targeted search added **{added}** item-specific hits "
                        f"({int(targeted.get('queries') or 0)} queries)"
                    )
                if filt.get("mode"):
                    caption += f" · mode `{filt['mode']}`"
                st.caption(caption)
            for warning in last_scan.get("warnings") or []:
                st.warning(warning)
            with st.expander("Adversarial debate: Bull vs Bear", expanded=False):
                b1, b2 = st.columns(2)
                b1.markdown("**Bull agent**")
                b1.write(last_scan.get("bull_overview") or "No bull overview recorded.")
                b2.markdown("**Bear agent**")
                b2.write(last_scan.get("bear_overview") or "No bear overview recorded.")
                counts = last_scan.get("feed_counts") or {}
                if counts:
                    st.caption(
                        " · ".join(f"{k.replace('_', ' ')}: {v}" for k, v in counts.items())
                    )
                if last_scan.get("modes"):
                    st.caption("Decoding modes: " + ", ".join(last_scan["modes"]))

    stats = database.performance_stats(days=30)
    cols = st.columns(4)
    cols[0].metric("Open signals", stats["open"])
    cols[1].metric("30D win rate", f"{stats['win_rate']:.0%}", help=f"{stats['closed']} closed trades")
    cols[2].metric("Avg realised P&L", f"{stats['avg_pnl_pct']:+.2f}%")
    open_signals = database.list_signals(status="open", limit=300)
    high_conviction = sum(1 for s in open_signals if (s.get("risk_tier") or "") == "HIGH")
    cols[3].metric("High-conviction", high_conviction, help="Confluence >= 0.65 with 2+ external streams")

    signals = database.list_signals(status=None, limit=300)
    if not signals:
        st.info("No signals yet. Run a scan from the sidebar to populate the desk.")
        return

    f1, f2, f3, f4, f5 = st.columns([1, 1, 1, 1, 2])
    status_filter = f1.selectbox("Status", ["all", "open", "won", "lost"], index=0)
    type_filter = f2.selectbox("Signal", ["all", "BUY", "SELL", "HOLD"], index=0)
    term_filter = f3.selectbox("Term", ["all", "SHORT", "LONG"], index=0)
    tier_filter = f4.selectbox("Risk tier", ["all", "HIGH", "MEDIUM", "LOW"], index=0)
    search = f5.text_input("Search item", placeholder="e.g. ENCHANTED_DIAMOND")

    filtered = [
        s
        for s in signals
        if (status_filter == "all" or s["status"] == status_filter)
        and (type_filter == "all" or s["signal"] == type_filter)
        and (
            term_filter == "all"
            or (s.get("term") or "SHORT").upper() == term_filter
        )
        and (tier_filter == "all" or (s.get("risk_tier") or "LOW") == tier_filter)
        and (not search or search.upper() in s["item_id"].upper())
    ]
    if not filtered:
        st.warning("No signals match the current filters.")
        return

    # Unit counts come from the purse: highest-confidence BUY calls are funded
    # first, each capped by the per-slot ceiling and the cash still free.
    sizes: dict[str, dict] = {}
    if purse > 0:
        stats = database.performance_stats(days=30)
        multiplier = 1.0 + (stats["win_rate"] - 0.5) * 0.6
        sizes = {
            (row["item_id"], row.get("term") or "SHORT"): row
            for row in size_book(filtered, purse, accuracy_multiplier=multiplier)
        }
        st.caption(
            f"Sized against **{_fmt_coins(purse)}** purse with the Balanced profile "
            "(15% per slot, 4 slots, 10% cash reserve). Adjust it in the Purse & Allocation tab."
        )
    else:
        st.caption("Set your purse in the sidebar to see how many units to buy per call.")

    rows = [
        {
            "Item": s["item_id"],
            "Term": (s.get("term") or "SHORT"),
            "Signal": s["signal"],
            "Conf": f"{s['confidence']:.0%}",
            "Tier": s.get("risk_tier") or "-",
            "Confluence": (
                f"{s['confluence_score']:.2f}" if s.get("confluence_score") is not None else "-"
            ),
            "Streams": len(s.get("sources") or []),
            "Entry": _fmt_coins(s["entry_price"]),
            "Units": (
                f"{sizes[(s['item_id'], s.get('term') or 'SHORT')]['units']:,}"
                if (s["item_id"], s.get("term") or "SHORT") in sizes
                else "-"
            ),
            "Capital": (
                _fmt_coins(sizes[(s["item_id"], s.get("term") or "SHORT")]["capital"])
                if (s["item_id"], s.get("term") or "SHORT") in sizes
                else "-"
            ),
            "Target": _fmt_coins(s["target_price"]),
            "Stop": _fmt_coins(s["stop_price"]),
            "Price/MA7": f"{s['risk_ratio']:.2f}x" if s["risk_ratio"] else "-",
            "Sector": s["sector"] or "-",
            "Horizon": f"{s['horizon_days'] or 14}d",
            "Expected date": s.get("expected_target_date") or "-",
            "Expected return": (
                f"{float(s['expected_return_pct']):+.1f}%"
                if s.get("expected_return_pct") is not None
                else "-"
            ),
            "Status": s["status"],
            "P&L": f"{s['pnl_pct']:+.2f}%" if s["pnl_pct"] is not None else "-",
            "Sources": "; ".join((s.get("sources") or [])[:3]) or "-",
            "Created": (s["created_at"] or "")[:16].replace("T", " "),
        }
        for s in filtered
    ]
    df = pd.DataFrame(rows)
    df["_ord"] = df["Signal"].map(SIGNAL_ORDER).fillna(9)
    df = df.sort_values(["_ord", "Created"], ascending=[True, False]).drop(columns="_ord")

    st.dataframe(df, use_container_width=True, hide_index=True, height=420)
    st.download_button(
        "Download screener CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="skyblock_signals.csv",
        mime="text/csv",
    )

    buys = [s for s in filtered if s["signal"] == "BUY" and s["status"] == "open"]
    if buys:
        st.markdown("#### Highest-conviction calls")
        for signal in sorted(buys, key=lambda s: s["confidence"], reverse=True)[:3]:
            sized = sizes.get((signal["item_id"], signal.get("term") or "SHORT"))
            allocation = (
                f" · buy **{sized['units']:,} units** ({_fmt_coins(sized['capital'])} coins)"
                if sized
                else ""
            )
            st.markdown(
                f"**{signal['item_id']}** · {signal.get('term') or 'SHORT'} · BUY @ "
                f"**{_fmt_coins(signal['entry_price'])}** "
                f"→ target **{_fmt_coins(signal['target_price'])}** "
                f"around **{signal.get('expected_target_date') or '-'}** "
                f"· confidence **{signal['confidence']:.0%}** · {signal['sector']}"
                + allocation
            )
