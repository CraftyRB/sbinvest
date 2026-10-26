"""SkyBlock AI Investment Dashboard - Streamlit entry point."""

from __future__ import annotations

import os

from dotenv import load_dotenv

# Every entry point loads secrets explicitly from env.txt.
load_dotenv("env.txt", override=False)

import streamlit as st

# Streamlit Community Cloud injects secrets via st.secrets, not env vars, so
# mirror them into the environment before config.py reads os.getenv().
try:
    for _key, _value in st.secrets.items():
        if isinstance(_value, str):
            os.environ.setdefault(_key, _value)
except Exception:
    pass

import config
import database
import pipeline
from intelligence import budget
from ui import calculator, consultant, longterm, portfolio, screener, thesis

st.set_page_config(
    page_title="SkyBlock AI Investment Desk",
    layout="wide",
    initial_sidebar_state="expanded",
)

CSS = """
<style>
    .block-container {padding-top: 1.8rem; padding-bottom: 3rem; max-width: 1450px;}
    .hero {padding: 1.1rem 1.4rem; border: 1px solid #1f2a36; border-radius: 16px;
           background: linear-gradient(135deg, #101923 0%, #0a0f15 55%, #101b26 100%);
           margin-bottom: 1.2rem;}
    .hero .kicker {font-size: .72rem; letter-spacing: .18em; color: #5f8fb8;
                   text-transform: uppercase; font-weight: 600;}
    .hero h1 {font-size: 1.85rem; margin: .25rem 0 .35rem 0; color: #e6edf3;}
    .hero p {margin: 0; color: #93a4b5; font-size: .93rem;}
    div[data-testid="stMetric"] {background: #111a23; border: 1px solid #1f2a36;
        border-radius: 12px; padding: 14px 16px;}
    div[data-testid="stMetricLabel"] {color: #8fa3b8;}
    div[data-testid="stSidebar"] {background: #0c131a; border-right: 1px solid #1c2732;}
    .stTabs [data-baseweb="tab-list"] {gap: 6px;}
    .stTabs [data-baseweb="tab"] {background: #111a23; border: 1px solid #1f2a36;
        border-radius: 10px 10px 0 0; padding: 8px 16px;}
    .stTabs [aria-selected="true"] {background: #1b2b3a; border-color: #2f81f7;}
    footer {visibility: hidden;}
</style>
"""

database.init_db()
st.markdown(CSS, unsafe_allow_html=True)

if "last_scan" not in st.session_state:
    st.session_state["last_scan"] = None

# --------------------------------------------------------------------------- #
# Sidebar controls
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.markdown("### Desk controls")
    st.caption(f"`{budget.model()}` · `{config.CEREBRAS_BASE_URL}`")

    # ---- Provider: swap key/model, watch the prepaid credit ----------------
    with st.expander("AI provider & spend", expanded=False):
        current_key = budget.api_key()
        active_model = budget.model()
        label_by_id = {mid: entry["label"] for mid, entry in budget.MODEL_CATALOG.items()}
        labels = list(label_by_id.values())
        active_label = label_by_id.get(active_model, labels[0])
        with st.form("ai_provider"):
            new_key = st.text_input(
                "Cerebras API key",
                value=current_key,
                type="password",
                help=(
                    "Stored locally in investments.db and used by both this dashboard and "
                    "the daemon on their next call. Saving a different key restarts the "
                    "balance at 5.00 USD."
                ),
            )
            chosen_label = st.selectbox(
                "Model",
                labels,
                index=labels.index(active_label),
                help=(
                    "Prices per million tokens: GPT-OSS 120B = 0.35 in / 0.75 out; "
                    "Qwen3 32B = 0.99 in / 1.49 out. Every completed call is priced from "
                    "the usage the API reports and deducted from the balance."
                ),
            )
            if st.form_submit_button("Save provider", use_container_width=True):
                if new_key.strip() and new_key.strip() != current_key:
                    budget.set_api_key(new_key)
                budget.set_model({v: k for k, v in label_by_id.items()}[chosen_label])
                st.rerun()

        snap = budget.snapshot()
        st.progress(snap["pct_used"])
        st.markdown(
            f"**{snap['remaining_usd']:.2f} USD left** of {snap['starting_usd']:.2f} USD\n\n"
            f"- Spent: {snap['spent_usd']:.3f} USD over {snap['requests']} calls\n"
            f"- Tokens: {snap['input_tokens']:,} in / {snap['output_tokens']:,} out\n"
            f"- Rate: {snap['input_rate']:.2f} in / {snap['output_rate']:.2f} out per M"
        )
        if snap["avg_cost_usd"]:
            calls_left = int(snap["remaining_usd"] / snap["avg_cost_usd"])
            st.caption(
                f"Avg {snap['avg_cost_usd']:.4f} USD per call · about {calls_left:,} calls "
                f"left at this burn."
            )
        if snap["remaining_usd"] < 0.5:
            st.warning("Balance nearly empty - top up to keep scans running.")
        with st.form("top_up"):
            credit = st.number_input(
                "Top up (USD)", min_value=0.5, value=5.0, step=0.5,
                help="Adds to the starting balance without wiping the spend history.",
            )
            if st.form_submit_button("Add credit", use_container_width=True):
                budget.add_credit(float(credit))
                st.rerun()
        recent = database.recent_llm_usage(limit=5, key_fp=snap["key_fp"])
        if recent:
            st.caption("Recent priced calls")
            for row in recent:
                st.caption(
                    f"{row['ts'][5:16].replace('T', ' ')} · {row['model']} · "
                    f"{(row['purpose'] or '')[:24]} · {row['cost_usd']:.4f} USD"
                )
    purse_millions = st.number_input(
        "Purse size (millions)",
        min_value=0.0,
        value=float(
            database.get_setting("last_purse", str(config.DEFAULT_PURSE_COINS))
            or config.DEFAULT_PURSE_COINS
        )
        / 1_000_000,
        step=5.0,
        format="%.1f",
        help=(
            "Your liquid coin balance in millions (e.g. 50 = 50M coins). It does not change "
            "the scan itself — it drives position sizing: unit counts in the Live Screener "
            "and the caps in the Purse & Allocation tab. Saved automatically, so the desk "
            "remembers it after a restart."
        ),
    )
    purse = float(purse_millions) * 1_000_000
    # Remember the purse across restarts (the consultant and sizing both read it).
    if int(purse) != int(float(database.get_setting("last_purse", "0") or 0)):
        database.set_setting("last_purse", str(int(purse)))

    with st.expander("Scan parameters", expanded=False):
        top_n = st.slider(
            "Candidates sent to AI",
            5,
            20,
            12,
            help=(
                "How many Bazaar items are handed to the Bull, Bear and Arbitrator agents. "
                "Half the slots go to the deepest dips vs their own moving average, half to "
                "the highest-volume names, so undervalued items are never crowded out. "
                "Higher = wider coverage but slower and more tokens; lower = faster, sharper."
            ),
        )
        min_volume_m = st.slider(
            "Min weekly volume (M coins)",
            0.0,
            50.0,
            0.25,
            0.25,
            help=(
                "Liquidity floor: an item must have traded at least this many million coins "
                "in the last 7 days to become a candidate. Filters thin books and pump-and-dump "
                "traps. Set to 0 to disable the filter."
            ),
        )
        commit_limit = st.slider(
            "NEU commits polled",
            1,
            8,
            3,
            help=(
                "How many of the newest NotEnoughUpdates-REPO commits to pull and diff. "
                "Commit diffs reveal unreleased item codes and recipe edits — the earliest "
                "alpha signal. Higher = more leak coverage, more GitHub API calls."
            ),
        )
        send_alert = st.checkbox(
            "Push alerts to Discord",
            value=False,
            help=(
                "After the scan, post the resulting BUY/HOLD/SELL book to the Discord webhook "
                "in env.txt. Off = no new-scan post. Exit alerts are separate: whenever a "
                "webhook is set, hitting a target or stop always pushes a 'ready to sell' "
                "message."
            ),
        )
        st.caption(
            "Purse size → allocation only · Candidates → AI breadth · "
            "Min volume → liquidity filter · Commits → leak lookback · "
            "Alerts → outbound push. These apply to the next scan."
        )

    if st.button("Run scan now", type="primary", use_container_width=True):
        st.session_state["last_scan"] = None
        with st.spinner("Polling 8 feeds and running the Bull/Bear/Arbiter debate..."):
            try:
                st.session_state["last_scan"] = pipeline.run_scan(
                    top_n=top_n,
                    commit_limit=commit_limit,
                    min_weekly_coins=min_volume_m * 1_000_000,
                    send_alert=send_alert,
                )
                st.success("Scan complete.")
            except Exception as exc:
                st.session_state["last_scan"] = {"error": str(exc)}
                st.error(f"Scan failed: {exc}")

    if st.button("Update evidence resources", use_container_width=True):
        with st.spinner("Refreshing feeds and searching evidence for the top candidates..."):
            try:
                st.session_state["resource_refresh"] = pipeline.refresh_resources(
                    top_n=top_n,
                    commit_limit=commit_limit,
                    min_weekly_coins=min_volume_m * 1_000_000,
                )
                st.success("Evidence resources updated.")
            except Exception as exc:
                st.session_state["resource_refresh"] = {"error": str(exc)}
                st.error(f"Refresh failed: {exc}")

    refresh = st.session_state.get("resource_refresh")
    if refresh:
        if refresh.get("error"):
            st.caption(f"Last refresh failed: {refresh['error']}")
        else:
            counts = refresh.get("feed_counts") or {}
            active = refresh.get("active_search") or {}
            added = sum(
                int(active.get(k) or 0) for k in ("reddit", "youtube", "wiki", "web")
            )
            st.caption(
                f"Evidence refreshed {refresh.get('refreshed_at', '')[:16].replace('T', ' ')} · "
                f"{sum(counts.values())} feed items · {added} targeted hits "
                f"({int(active.get('queries') or 0)} queries)"
            )
            for warning in refresh.get("warnings") or []:
                st.caption(warning)
    st.caption(
        "Run scan = feeds + AI debate + new signals. "
        "Update evidence resources = refresh prices, feeds and targeted search only (no AI)."
    )

    st.divider()
    stats = database.performance_stats(days=30)
    threshold = database.get_setting("last_threshold")
    st.caption("**Feedback loop**")
    st.markdown(
        f"- Win rate (30D): **{stats['win_rate']:.0%}** over {stats['closed']} closed\n"
        f"- Avg realised P&L: **{stats['avg_pnl_pct']:+.2f}%**\n"
        f"- Open signals: **{stats['open']}**\n"
        f"- Buy threshold: **{threshold or '0.92'}x** MA7"
    )

    daemon_status = database.get_setting("daemon_status", "offline")
    daemon_summary = database.get_setting("daemon_last_summary", "no cycle yet")
    daemon_cycles = database.get_setting("daemon_cycles", "0")
    last_scan_at = database.get_setting("last_scan_at", "never")
    st.caption("**Daemon**")
    st.markdown(
        f"- Status: **{daemon_status}** · cycles {daemon_cycles}\n"
        f"- Last scan: {last_scan_at[:16].replace('T', ' ')}\n"
        f"- {daemon_summary}"
    )

    health = database.model_health()
    st.caption("**System**")
    st.markdown(
        f"- Hypixel key: {'set' if config.HYPIXEL_API_KEY else 'missing'}\n"
        f"- Cerebras key: {config.mask(budget.api_key())}\n"
        f"- Model calls: {health['runs']} · success {health['success_rate']:.0%}\n"
        f"- Latency: {health['avg_latency_ms']} ms\n"
        f"- DB: `{config.DB_PATH.name}`"
    )

# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
st.markdown(
    """
    <div class="hero">
        <div class="kicker">Hypixel SkyBlock · Quantitative Desk</div>
        <h1>SkyBlock AI Investment Dashboard</h1>
        <p>Eight-feed alpha ingestion, adversarial Bull/Bear debate and a self-correcting win-rate feedback loop.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

# Persist the adaptive threshold for the sidebar read-out.
if st.session_state["last_scan"] and not st.session_state["last_scan"].get("error"):
    database.set_setting(
        "last_threshold", f"{st.session_state['last_scan']['threshold']['buy_threshold']:.2f}"
    )

last_scan = st.session_state["last_scan"]

tab_screen, tab_long, tab_thesis, tab_calc, tab_portfolio, tab_ask = st.tabs(
    [
        "Live Screener",
        "Long-Term Research",
        "Thesis Inspector",
        "Purse & Allocation",
        "Portfolio Report",
        "Ask the Desk",
    ]
)

with tab_screen:
    screener.render(last_scan, purse)

with tab_long:
    longterm.render(last_scan)

with tab_thesis:
    thesis.render()

with tab_calc:
    calculator.render(purse, last_scan)

with tab_portfolio:
    portfolio.render(purse)

with tab_ask:
    consultant.render(purse)

st.divider()
st.caption(
    "Hypixel SkyBlock items are virtual goods with no monetary value. This dashboard is a "
    "research tool, not financial advice. Bazaar prices are volatile and liquidity can vanish."
)
