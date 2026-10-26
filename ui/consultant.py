"""Ask-the-desk tab: follow-up Q&A on the last scan via the Cerebras model."""

from __future__ import annotations

import streamlit as st

from intelligence import consultant
from intelligence.client import IntelligenceError

SUGGESTIONS = (
    "Why is the deepest dip only a HOLD?",
    "How many units should I buy of each open BUY?",
    "What would make you exit the top BUY early?",
    "Which candidate has the strongest evidence, and why?",
)


def _fmt(value) -> str:
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return "-"


def _answer(question: str, purse: float, chat: list[dict]) -> None:
    chat.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        with st.spinner("Consultant is reading the analysis..."):
            try:
                reply = consultant.ask(question, chat[:-1], purse=purse)
            except IntelligenceError as exc:
                reply = f"Model call failed: {exc}"
                st.error(reply)
        if not reply.startswith("Model call failed"):
            st.markdown(reply)
    chat.append({"role": "assistant", "content": reply})


def render(purse: float = 0.0) -> None:
    st.subheader("Ask the desk")
    st.caption(
        "A Cerebras-backed consultant that answers follow-up questions from the last "
        "analysis — prices, sizing, risk, catalysts, or why a call was made. It only "
        "uses stored scan data and never invents prices."
    )

    info = consultant.context_summary(purse)
    purse = purse or info["purse"]
    if not info["has_snapshot"]:
        st.info(
            "No analysis snapshot on record yet. Run a scan (or let the daemon finish a "
            "cycle) and the consultant will have something to talk about."
        )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Last scan", (info["scanned_at"] or "never")[:16].replace("T", " "))
    c2.metric("Open positions", info["open_signals"])
    c3.metric("Candidates analysed", info["candidates"])
    c4.metric("Purse", f"{purse / 1_000_000:.1f}M" if purse else "not set")

    threshold = info.get("threshold") or {}
    if threshold:
        st.caption(
            f"Buy threshold **{threshold.get('buy_threshold', '-')}x MA7** · "
            "intraday gate **0.985x** and **−0.5σ** · sizing uses the Balanced profile"
        )
    if info["market_view"]:
        with st.expander("Last market view", expanded=False):
            st.write(info["market_view"])

    st.divider()

    chat: list[dict] = st.session_state.setdefault("desk_chat", [])

    if not chat:
        st.caption("Try one of these, or ask your own question below.")
        cols = st.columns(2)
        for index, suggestion in enumerate(SUGGESTIONS):
            if cols[index % 2].button(
                suggestion, key=f"desk_suggestion_{index}", use_container_width=True
            ):
                st.session_state["desk_pending"] = suggestion
                st.rerun()

    for message in chat:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    pending = st.session_state.pop("desk_pending", None)
    if pending:
        _answer(pending, purse, chat)
        st.rerun()

    prompt = st.chat_input("Ask about a signal, an item, sizing or risk...")
    if prompt:
        if purse <= 0:
            st.warning("Set your purse in the sidebar for unit sizing.")
        _answer(prompt, purse, chat)
        st.rerun()

    if chat:
        if st.button("Clear conversation", use_container_width=False):
            st.session_state["desk_chat"] = []
            st.rerun()
