"""Portfolio Master Report tab."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

import config
import database
import pipeline
from analytics import portfolio_summary, statement_csv, value_holdings


def _coins(value: float) -> str:
    return f"{value:,.0f}"


def _cashouts_markdown(lines: list[str], cashouts: list[dict]) -> None:
    lines += ["", "## Realised cash-outs", ""]
    if not cashouts:
        lines.append("_Nothing sold yet._")
        return
    lines.append("| Item | Units | Sold at | Proceeds | Cost basis | P&L | P&L % | When |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---|")
    for sale in cashouts:
        qty = float(sale["qty"])
        price = float(sale["price"])
        lines.append(
            f"| {sale['item_id']} | {qty:,.0f} | {price:,.0f} | {qty * price:,.0f} | "
            f"{float(sale['cost_basis']):,.0f} | {float(sale['pnl']):+,.0f} | "
            f"{float(sale['pnl_pct']):+.2f}% | {(sale['sold_at'] or '')[:16].replace('T', ' ')} |"
        )
    realised = sum(float(s["pnl"]) for s in cashouts)
    lines.append("")
    lines.append(f"**Realised P&L:** {realised:+,.0f} coins over {len(cashouts)} sale(s).")


def _cashouts_table(cashouts: list[dict]) -> None:
    if not cashouts:
        st.caption("No cash-outs recorded yet.")
        return
    realised = sum(float(s["pnl"]) for s in cashouts)
    st.caption(
        f"**Realised P&L:** {realised:+,.0f} coins over {len(cashouts)} sale(s). "
        "Each sale reduced the holding and credited the purse."
    )
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Item": s["item_id"],
                    "Units": f"{float(s['qty']):,.0f}",
                    "Sold at": f"{float(s['price']):,.0f}",
                    "Proceeds": f"{float(s['qty']) * float(s['price']):,.0f}",
                    "Cost basis": f"{float(s['cost_basis']):,.0f}",
                    "P&L": f"{float(s['pnl']):+,.0f}",
                    "P&L %": f"{float(s['pnl_pct']):+.2f}%",
                    "When": (s["sold_at"] or "")[:16].replace("T", " "),
                }
                for s in cashouts
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )
    with st.expander("Delete a cash-out"):
        labels = {
            f"#{s['id']} · {s['item_id']} · {float(s['qty']):,.0f} @ {float(s['price']):,.0f}": s["id"]
            for s in cashouts
        }
        pick = st.selectbox("Entry", list(labels.keys()), key="delete_cashout_pick")
        st.caption(
            "Removes the entry from the statement log only. The holding reduction and "
            "purse credit from that sale already happened and are left as they are."
        )
        if st.button("Delete cash-out"):
            database.delete_cashout(labels[pick])
            st.rerun()


def _statement_markdown(
    portfolio: dict,
    rows: list[dict],
    summary: dict,
    generated_at: str,
    orders: list[dict] | None = None,
    cashouts: list[dict] | None = None,
) -> str:
    lines = [
        "# SkyBlock Portfolio Statement",
        "",
        f"**Portfolio:** {portfolio['name']}  ",
        f"**Generated:** {generated_at}  ",
        f"**Purse:** {summary['purse']:,.0f} coins  ",
        f"**Invested cost:** {summary['invested_cost']:,.0f}  ",
        f"**Market value:** {summary['market_value']:,.0f}  ",
        f"**Unrealised P&L:** {summary['unrealized_pnl']:+,.0f} "
        f"({summary['unrealized_pnl_pct']:+.2f}%)  ",
        f"**Cash:** {summary['cash']:,.0f}",
        "",
        "## Holdings",
        "",
        "| Item | Sector | Qty | Cost basis | Market | Cost | Value | P&L | P&L % |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['item_id']} | {r['sector']} | {r['qty']:,} | {r['cost_basis']:,.0f} | "
            f"{r['market_price']:,.0f} | {r['cost']:,.0f} | {r['value']:,.0f} | "
            f"{r['pnl']:+,.0f} | {r['pnl_pct']:+.2f}% |"
        )
    if not rows:
        lines.append("| _No holdings_ | - | - | - | - | - | - | - | - |")

    lines += ["", "## Sector allocation", ""]
    for sector, pct in summary["sector_allocation"].items():
        lines.append(f"- {sector}: {pct:.2f}%")

    lines += ["", "## Cash-out triggers", ""]
    if rows:
        lines.append(
            "Each holding sends a Discord alert once when it crosses a take-profit "
            "or stop tier, so nothing needs to be monitored by hand."
        )
        lines.append("")
        lines.append(
            f"| Item | Cost basis | Sell at +{config.PORTFOLIO_TAKE_PROFIT_PCT:.0%} | "
            f"Sell bulk at +{config.PORTFOLIO_STRONG_TAKE_PROFIT_PCT:.0%} | "
            f"Stop at -{config.PORTFOLIO_STOP_LOSS_PCT:.0%} | Now |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|")
        for r in rows:
            basis = float(r["cost_basis"])
            lines.append(
                f"| {r['item_id']} | {basis:,.0f} | "
                f"{basis * (1 + config.PORTFOLIO_TAKE_PROFIT_PCT):,.0f} | "
                f"{basis * (1 + config.PORTFOLIO_STRONG_TAKE_PROFIT_PCT):,.0f} | "
                f"{basis * (1 - config.PORTFOLIO_STOP_LOSS_PCT):,.0f} | "
                f"{r['market_price']:,.0f} ({r['pnl_pct']:+.2f}%) |"
            )
    else:
        lines.append("_No holdings yet._")

    lines += ["", "## Planned partial exits", ""]
    if orders:
        lines.append("| Item | Units | Sell at | Bid now | Distance | Status |")
        lines.append("|---|---:|---:|---:|---:|---|")
        bid = {r["item_id"]: r["market_price"] for r in rows}
        for order in orders:
            current = bid.get(order["item_id"])
            distance = (
                f"{(float(order['target_price']) / current - 1) * 100:+.2f}%"
                if current
                else "-"
            )
            lines.append(
                f"| {order['item_id']} | {float(order['qty']):,.0f} | "
                f"{float(order['target_price']):,.0f} | {current or 0:,.0f} | "
                f"{distance} | {order['status']} |"
            )
    else:
        lines.append("_No partial exits planned._")

    _cashouts_markdown(lines, cashouts or [])

    lines += [
        "",
        "## Methodology",
        "",
        "- Market prices mark holdings to the latest Bazaar snapshot, liquidating at the top bid.",
        "- P&L excludes Bazaar fees and assumes the full position can exit at that bid.",
        "- Signals and outcomes are stored in `investments.db`; realised results drive the adaptive threshold.",
    ]
    return "\n".join(lines)


def render(sidebar_purse: float) -> None:
    st.subheader("Portfolio Master Report")
    st.caption("Holdings, unrealised P&L and sector allocation, exportable as a statement.")

    portfolios = database.list_portfolios()
    if not portfolios:
        st.info("Create a portfolio to start tracking holdings.")
        with st.form("create_portfolio"):
            name = st.text_input("Portfolio name", value="Main")
            purse = st.number_input("Purse (coins)", min_value=0.0, value=float(sidebar_purse), step=1_000_000.0)
            if st.form_submit_button("Create portfolio", type="primary"):
                database.create_portfolio(name.strip() or "Main", purse)
                st.rerun()
        return

    names = {p["name"]: p for p in portfolios}
    c1, c2 = st.columns([2, 1])
    selected_name = c1.selectbox("Portfolio", list(names.keys()))
    portfolio = names[selected_name]

    new_purse = c2.number_input(
        "Purse (coins)",
        min_value=0.0,
        value=float(portfolio["purse"]),
        step=1_000_000.0,
        key=f"purse_{portfolio['id']}",
    )
    if new_purse != portfolio["purse"]:
        database.update_purse(portfolio["id"], new_purse)
        portfolio["purse"] = new_purse

    with st.expander("Add a holding"):
        with st.form("add_holding", clear_on_submit=True):
            h1, h2, h3 = st.columns(3)
            item_id = h1.text_input("Item id", placeholder="ENCHANTED_DIAMOND").upper().strip()
            qty = h2.number_input("Quantity", min_value=0.0, value=1.0, step=1.0)
            cost = h3.number_input("Cost basis (per unit)", min_value=0.0, value=0.0, step=1_000.0)
            if st.form_submit_button("Add holding"):
                if not item_id or qty <= 0 or cost <= 0:
                    st.error("Item id, quantity and cost basis are required.")
                else:
                    database.add_holding(portfolio["id"], item_id, qty, cost)
                    st.rerun()

    if st.button("Refresh market prices"):
        try:
            with st.spinner("Pulling Bazaar snapshot..."):
                saved = pipeline.refresh_prices()
            st.success(f"Saved {saved:,} price rows.")
            st.rerun()
        except Exception as exc:
            st.error(f"Price refresh failed: {exc}")

    channel = "portfolio webhook" if config.DISCORD_PORTFOLIO_WEBHOOK_URL else "webhook not configured"
    st.caption(
        f"Cash-out alerts go to your **{channel}** (separate from scan alerts): "
        f"+{config.PORTFOLIO_TAKE_PROFIT_PCT:.0%} sell, "
        f"+{config.PORTFOLIO_STRONG_TAKE_PROFIT_PCT:.0%} sell the bulk, "
        f"-{config.PORTFOLIO_STOP_LOSS_PCT:.0%} stop. Each tier fires once per crossing."
    )
    last_alerts = database.get_setting("last_portfolio_alerts")
    if last_alerts:
        try:
            payload = json.loads(last_alerts)
        except ValueError:
            payload = {}
        if payload.get("sent"):
            st.caption(
                f"Last cash-out push: **{payload['sent']}** alert(s) at "
                f"{(payload.get('at') or '')[:16].replace('T', ' ')}."
            )

    holdings = database.list_holdings(portfolio["id"])
    prices = database.latest_prices()
    rows = value_holdings(holdings, prices)
    summary = portfolio_summary(rows, float(portfolio["purse"]))
    cashouts = database.list_cashouts(portfolio["id"])

    m = st.columns(5)
    m[0].metric("Market value", _coins(summary["market_value"]))
    m[1].metric("Invested cost", _coins(summary["invested_cost"]))
    m[2].metric("Unrealised P&L", _coins(summary["unrealized_pnl"]), f"{summary['unrealized_pnl_pct']:+.2f}%")
    m[3].metric("Cash", _coins(summary["cash"]))
    m[4].metric("Holdings", len(rows))

    if not holdings:
        st.info("No holdings yet. Add one above and refresh prices to mark it to market.")
        st.markdown("#### Realised cash-outs")
        _cashouts_table(cashouts)
        return

    table = pd.DataFrame(
        [
            {
                "Item": r["item_id"],
                "Sector": r["sector"],
                "Qty": f"{r['qty']:,}",
                "Cost basis": f"{r['cost_basis']:,.0f}",
                "Market": f"{r['market_price']:,.0f}",
                "Cost": f"{r['cost']:,.0f}",
                "Value": f"{r['value']:,.0f}",
                "P&L": f"{r['pnl']:+,.0f}",
                "P&L %": f"{r['pnl_pct']:+.2f}%",
                "Priced at": r["as_of"][:16].replace("T", " "),
            }
            for r in rows
        ]
    )
    st.dataframe(table, use_container_width=True, hide_index=True)

    # ---- Remove a holding entirely -------------------------------------------
    with st.expander("Remove a holding"):
        pick = st.selectbox(
            "Holding to remove",
            list(range(len(rows))),
            format_func=lambda i: (
                f"{rows[i]['item_id']} · {rows[i]['qty']:g} held @ {rows[i]['cost_basis']:,.0f}"
            ),
            key=f"remove_holding_{portfolio['id']}",
        )
        target = rows[pick]
        st.caption(
            "Removes this position from the portfolio and cancels its planned partial "
            "exits. Cash-outs already logged stay in the realised table."
        )
        if st.button(
            f"Remove {target['item_id']} ({target['qty']:g} units)",
            key=f"remove_holding_btn_{portfolio['id']}",
        ):
            database.delete_holding(int(target["id"]))
            st.rerun()

    # ---- Book an actual sale: shrink the holding, credit the purse ----------
    with st.expander("Record a cash-out"):
        st.caption(
            "Book a sale you just filled. The holding shrinks (or closes), the "
            "proceeds are credited to your purse and the realised P&L is logged below."
        )
        sale_choices = {
            f"{r['item_id']} · {r['qty']:,} held @ {r['cost_basis']:,.0f}": r for r in rows
        }
        sale_pick = st.selectbox(
            "Holding to sell", list(sale_choices.keys()), key="cashout_holding"
        )
        sold = sale_choices[sale_pick]
        # Seed the editable fields only when the chosen holding changes. The
        # widgets keep their own session state afterwards, so typed values are
        # never overwritten by a background rerun.
        if st.session_state.get("cashout_seed_for") != sold["id"]:
            st.session_state["cashout_units"] = float(min(1.0, sold["qty"]))
            st.session_state["cashout_price"] = float(round(sold["market_price"]))
            st.session_state["cashout_seed_for"] = sold["id"]
        with st.form("record_cashout", clear_on_submit=False):
            s2, s3 = st.columns(2)
            sell_units = s2.number_input(
                "Units sold",
                min_value=0.0,
                max_value=float(sold["qty"]),
                step=1.0,
                format="%.0f",
                key="cashout_units",
            )
            sell_price = s3.number_input(
                "Sold at (per unit)",
                min_value=0.0,
                step=1.0,
                format="%.0f",
                key="cashout_price",
            )
            if st.form_submit_button("Book sale"):
                try:
                    database.record_cashout(
                        portfolio["id"], sold["id"], sell_units, sell_price
                    )
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    for key in ("cashout_units", "cashout_price", "cashout_seed_for"):
                        st.session_state.pop(key, None)
                    st.rerun()

    # ---- Planned partial exits: sell N units at a chosen price --------------
    orders = database.list_exit_orders(portfolio["id"], status="pending")
    st.markdown("#### Planned partial exits")
    st.caption(
        "Set how many units of a holding you want to sell and at what price. "
        "The portfolio webhook alerts you once when the bid reaches that price."
    )
    holding_choices = {
        f"{r['item_id']} · {r['qty']:,} held @ {r['cost_basis']:,.0f}": r for r in rows
    }
    chosen = st.selectbox("Holding", list(holding_choices.keys()), key="exit_holding")
    picked = holding_choices[chosen]
    # Seed defaults only when the holding changes; typed values then live in
    # session state so background reruns cannot snap them back.
    if st.session_state.get("exit_seed_for") != picked["id"]:
        st.session_state["exit_units"] = float(min(1.0, picked["qty"]))
        st.session_state["exit_price"] = float(round(picked["market_price"] * 1.1))
        st.session_state["exit_seed_for"] = picked["id"]
    with st.form("add_exit_order", clear_on_submit=False):
        e2, e3 = st.columns(2)
        units = e2.number_input(
            "Units to sell",
            min_value=0.0,
            max_value=float(picked["qty"]),
            step=1.0,
            format="%.0f",
            key="exit_units",
        )
        exit_price = e3.number_input(
            "Sell price (per unit)",
            min_value=0.0,
            step=1.0,
            format="%.0f",
            key="exit_price",
        )
        if st.form_submit_button("Add partial exit"):
            planned_units = sum(
                float(o["qty"]) for o in orders if o["holding_id"] == picked["id"]
            )
            if units <= 0 or exit_price <= 0:
                st.error("Units and sell price must both be greater than zero.")
            elif planned_units + units > float(picked["qty"]):
                st.error(
                    f"Only {picked['qty']:,} units held; "
                    f"{planned_units:,.0f} already planned for this holding."
                )
            else:
                database.add_exit_order(
                    portfolio["id"], picked["id"], picked["item_id"], units, exit_price
                )
                st.rerun()

    if orders:
        bid_by_item = {r["item_id"]: r["market_price"] for r in rows}
        order_table = pd.DataFrame(
            [
                {
                    "Item": o["item_id"],
                    "Units": f"{o['qty']:,.0f}",
                    "Sell at": f"{o['target_price']:,.0f}",
                    "Bid now": f"{bid_by_item.get(o['item_id'], 0):,.0f}",
                    "Distance to exit": (
                        f"{(float(o['target_price']) / bid_by_item[o['item_id']] - 1) * 100:+.2f}%"
                        if bid_by_item.get(o["item_id"])
                        else "-"
                    ),
                    "Status": o["status"],
                    "Created": (o["created_at"] or "")[:16].replace("T", " "),
                }
                for o in orders
            ]
        )
        st.dataframe(order_table, use_container_width=True, hide_index=True)
        planned_by_item: dict[str, float] = {}
        for o in orders:
            planned_by_item[o["item_id"]] = planned_by_item.get(o["item_id"], 0.0) + float(o["qty"])
        st.caption(
            " · ".join(
                f"{item}: {qty:,.0f} units planned" for item, qty in planned_by_item.items()
            )
        )
        with st.expander("Cancel a partial exit"):
            labels = {
                f"#{o['id']} · {o['item_id']} · {o['qty']:,.0f} @ {o['target_price']:,.0f}": o["id"]
                for o in orders
            }
            pick = st.selectbox("Order", list(labels.keys()))
            if st.button("Delete exit order"):
                database.delete_exit_order(labels[pick])
                st.rerun()
    else:
        st.caption("No partial exits planned yet.")

    st.markdown("#### Realised cash-outs")
    _cashouts_table(cashouts)

    if summary["sector_allocation"]:
        st.markdown("#### Sector allocation (% of market value)")
        st.bar_chart(
            pd.DataFrame({"Weight": list(summary["sector_allocation"].values())},
                         index=list(summary["sector_allocation"].keys())),
            height=240,
        )

    generated_at = database.utcnow()
    st.download_button(
        "Download statement (CSV)",
        data=statement_csv(rows, summary).encode("utf-8"),
        file_name=f"skyblock_statement_{portfolio['name'].lower().replace(' ', '_')}.csv",
        mime="text/csv",
    )
    st.download_button(
        "Download master report (Markdown)",
        data=_statement_markdown(portfolio, rows, summary, generated_at, orders, cashouts).encode("utf-8"),
        file_name=f"skyblock_report_{portfolio['name'].lower().replace(' ', '_')}.md",
        mime="text/markdown",
    )

    with st.expander("Remove a holding"):
        options = {f"#{h['id']} · {h['item_id']} · {h['qty']:,} @ {h['cost_basis']:,.0f}": h["id"] for h in holdings}
        choice = st.selectbox("Holding", list(options.keys()))
        if st.button("Delete holding"):
            database.delete_holding(options[choice])
            st.rerun()
