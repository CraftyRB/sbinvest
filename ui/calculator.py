"""Purse & Allocation Calculator tab."""

from __future__ import annotations

import math

import pandas as pd
import streamlit as st

import database
from analytics import allocation_plan

PRESETS = {
    "Conservative": {"max_position_pct": 0.08, "reserve_pct": 0.20, "active_slots": 3},
    "Balanced": {"max_position_pct": 0.15, "reserve_pct": 0.10, "active_slots": 4},
    "Aggressive": {"max_position_pct": 0.25, "reserve_pct": 0.05, "active_slots": 5},
}


def _coins(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,.0f}"


def render(sidebar_purse: float, last_scan: dict | None) -> None:
    st.subheader("Purse & Allocation Calculator")
    st.caption("Position sizing from your liquid purse, with a hard cash reserve.")

    c1, c2 = st.columns([1.4, 1])
    purse_millions = c1.number_input(
        "Purse size (millions)",
        min_value=0.0,
        value=float(sidebar_purse) / 1_000_000,
        step=5.0,
        format="%.1f",
        key="calc_purse",
        help="Enter 50 for 50M coins.",
    )
    purse = float(purse_millions) * 1_000_000
    preset_name = c2.selectbox("Risk profile", list(PRESETS.keys()), index=1)
    preset = PRESETS[preset_name]

    if purse <= 0:
        st.info("Enter your purse size to size positions.")
        return

    stats = database.performance_stats(days=30)
    # Performance-scaled sizing: a hot streak allows slightly larger positions,
    # a losing streak shrinks them. Clamped to 0.5x-1.3x inside allocation_plan.
    accuracy_multiplier = 1.0 + (stats["win_rate"] - 0.5) * 0.6
    plan = allocation_plan(purse=purse, accuracy_multiplier=accuracy_multiplier, **preset)

    m = st.columns(5)
    m[0].metric("Max per trade", _coins(plan["max_per_trade"]), help=f"{plan['max_position_pct']:.0%} of deployable")
    m[1].metric("Active slots", plan["active_slots"])
    m[2].metric("Total deployed", _coins(plan["total_deployed"]))
    m[3].metric("Cash reserve", _coins(plan["reserve"]))
    m[4].metric(
        "Accuracy multiplier",
        f"{plan['accuracy_multiplier']:.2f}x",
        help=f"30D win rate {stats['win_rate']:.0%} over {stats['closed']} closed trades",
    )

    chart = pd.DataFrame(
        {
            "Coins": [
                plan["max_per_trade"] * plan["active_slots"],
                plan["reserve"],
                max(plan["cash_left"] - plan["reserve"], 0.0),
            ]
        },
        index=["Deployed capital", "Cash reserve", "Unallocated cash"],
    )
    st.bar_chart(chart, height=220)

    st.markdown("#### Size the current book")
    if not last_scan or last_scan.get("error"):
        st.caption("Run a scan to size live signals against your purse.")
    else:
        actionable = [
            s for s in last_scan.get("signals", [])
            if s["signal"] in ("BUY", "SELL") and s["confidence"] >= 0.55
        ][:6]
        if not actionable:
            st.caption("No actionable BUY/SELL signals in the latest scan.")
        else:
            rows = []
            for s in actionable:
                entry = float(s["entry_price"] or 0)
                qty = math.floor(plan["max_per_trade"] / entry) if entry else 0
                deployed = qty * entry
                target_profit = qty * max(float(s["target_price"]) - entry, 0)
                stop_loss = qty * max(entry - float(s["stop_price"]), 0)
                rows.append(
                    {
                        "Item": s["item_id"],
                        "Signal": s["signal"],
                        "Entry": f"{entry:,.0f}",
                        "Qty": f"{qty:,}",
                        "Capital": f"{deployed:,.0f}",
                        "Profit @ target": f"{target_profit:,.0f}",
                        "Risk @ stop": f"{stop_loss:,.0f}",
                        "R:R": (
                            f"{target_profit / stop_loss:.2f}x" if stop_loss > 0 else "-"
                        ),
                    }
                )
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            st.caption(
                f"Each row is capped at {_coins(plan['max_per_trade'])} "
                f"({plan['max_position_pct']:.0%} of {_coins(plan['deployable'])} deployable)."
            )

    st.markdown("#### Quick sizer")
    q1, q2 = st.columns(2)
    unit_price = q1.number_input("Item price (coins)", min_value=0.0, value=100_000.0, step=1_000.0)
    if unit_price > 0:
        qty = math.floor(plan["max_per_trade"] / unit_price)
        q2.metric("Quantity within one slot", f"{qty:,}")
        st.caption(
            f"{qty:,} units × {unit_price:,.0f} = {qty * unit_price:,.0f} coins committed, "
            f"{plan['max_per_trade'] - qty * unit_price:,.0f} coins left in the slot."
        )
