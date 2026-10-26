"""Investment Thesis Inspector tab."""

from __future__ import annotations

import streamlit as st

import database


def _coins(value: float | None) -> str:
    return f"{value:,.0f}" if value is not None else "-"


def render() -> None:
    st.subheader("Investment Thesis Inspector")
    st.caption("Open a signal to read the full institutional reasoning and close the trade.")

    signals = database.list_signals(status=None, limit=200)
    if not signals:
        st.info("No signals available yet. Run a scan first.")
        return

    labels = {
        f"{s['item_id']} · {s['signal']} · {(s['created_at'] or '')[:16]} · #{s['id']}": s
        for s in signals
    }
    choice = st.selectbox("Select a signal", list(labels.keys()))
    signal = labels[choice]
    prices = database.latest_prices()
    live = prices.get(signal["item_id"])

    with st.container(border=True):
        header = st.columns([3, 1])
        header[0].markdown(f"### {signal['item_name']}  ·  `{signal['signal']}`")
        header[0].caption(
            f"{signal.get('term') or 'SHORT'}-term · {signal['sector'] or 'OTHER'} · "
            f"horizon {signal['horizon_days'] or 14}d · "
            f"source **{signal['source']}** · status **{signal['status']}**"
        )
        header[1].metric("Confidence", f"{signal['confidence']:.0%}")

        m = st.columns(6)
        m[0].metric("Entry", _coins(signal["entry_price"]))
        m[1].metric("Target", _coins(signal["target_price"]))
        m[2].metric("Stop", _coins(signal["stop_price"]))
        m[3].metric(
            "Price / 7D MA",
            f"{signal['risk_ratio']:.2f}x" if signal["risk_ratio"] else "n/a",
        )
        m[4].metric("Risk tier", signal.get("risk_tier") or "-")
        m[5].metric(
            "Confluence",
            f"{signal['confluence_score']:.2f}"
            if signal.get("confluence_score") is not None
            else "-",
        )

        # The self-learning forecast: when is the return expected, and how did
        # past forecasts of this term actually resolve?
        if signal.get("expected_target_date"):
            st.markdown(
                f"**Forecast** · expected return "
                f"**{float(signal.get('expected_return_pct') or 0):+.1f}%** in about "
                f"**{signal.get('expected_days_to_target') or '-'} days** "
                f"(around **{signal['expected_target_date']}**) · method "
                f"`{signal.get('forecast_method') or '-'}`"
                + (
                    f" · catalyst date **{signal['catalyst_date']}**"
                    if signal.get("catalyst_date")
                    else ""
                )
            )
        if signal.get("grade"):
            st.caption(
                f"Graded **{signal['grade']}** on "
                f"{(signal.get('graded_at') or '')[:16].replace('T', ' ')}"
            )

        if live:
            upside = (
                (signal["target_price"] / live["buy_price"] - 1) * 100
                if signal["target_price"] and live["buy_price"]
                else 0.0
            )
            st.caption(
                f"Live bid **{live['buy_price']:,.0f}** / ask **{live['sell_price']:,.0f}** "
                f"(snapshot {live['ts'][:16].replace('T', ' ')}) · "
                f"upside to target at bid: **{upside:+.2f}%**"
            )

        st.markdown("#### Thesis")
        st.write(signal["thesis"] or "No thesis recorded.")

        bull_col, bear_col = st.columns(2)
        with bull_col:
            st.markdown("#### Bull case")
            st.write(signal.get("bull_case") or "Bull agent did not file a case for this ticker.")
        with bear_col:
            st.markdown("#### Bear case")
            st.write(signal.get("bear_case") or "Bear agent did not file a rebuttal for this ticker.")

        if signal.get("sources"):
            st.markdown("#### Evidence streams")
            for label in signal["sources"]:
                st.markdown(f"- {label}")

        if signal["catalysts"]:
            st.markdown("#### Catalysts")
            for catalyst in signal["catalysts"]:
                st.markdown(f"- {catalyst}")

        if signal["commit_sha"]:
            st.caption(f"Grounded in NEU commit `{signal['commit_sha'][:12]}`")

        if signal["status"] == "open":
            st.divider()
            st.markdown("#### Close position (feedback loop)")
            st.caption(
                "Realised outcomes update the win-rate, which shifts the adaptive buy threshold "
                "for future scans."
            )
            default_exit = float(live["buy_price"] if live else signal["entry_price"] or 0)
            c1, c2 = st.columns([2, 1])
            exit_price = c1.number_input(
                "Exit price (coins)",
                min_value=0.0,
                value=default_exit,
                step=max(default_exit * 0.01, 1.0),
                key=f"exit_{signal['id']}",
            )
            if c2.button("Mark closed", key=f"resolve_{signal['id']}", use_container_width=True):
                try:
                    result = database.resolve_signal(signal["id"], exit_price)
                    st.success(
                        f"Signal #{signal['id']} closed as **{result['status']}** "
                        f"at {result['pnl_pct']:+.2f}%."
                    )
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        else:
            st.info(
                f"Closed as **{signal['status']}** · exit {_coins(signal['exit_price'])} "
                f"· realised {signal['pnl_pct']:+.2f}%"
                if signal["pnl_pct"] is not None
                else f"Closed as **{signal['status']}**"
            )

    st.divider()
    st.markdown("#### Early leak feed (GitHub · NotEnoughUpdates-REPO)")
    leaks = database.recent_leaks(limit=6)
    if not leaks:
        st.caption("No leak commits cached yet. They are pulled on each scan.")
    for leak in leaks:
        with st.expander(f"{(leak['ts'] or '')[:10]} · {leak['title'] or leak['sha'][:8]}"):
            if leak["items"]:
                st.markdown("**Changed item files:** " + ", ".join(leak["items"][:12]))
            st.code((leak["summary"] or "")[:800], language="text")
            if leak["diff"]:
                st.code(leak["diff"][:4000], language="diff")

    st.divider()
    st.markdown("#### Evidence used for this signal")
    st.caption(
        "Only evidence the AI relevance gate kept for this ticker. Labels show the stream, "
        "relevance score and why it matters."
    )
    used = signal.get("sources") or []
    if used:
        for label in used:
            st.markdown(f"- {label}")
    else:
        st.caption(
            "No external evidence was relevant — this is a pure quantitative screen. "
            "The raw feed cache below is unfiltered and may contain unrelated chatter."
        )

    st.divider()
    st.markdown("#### Unfiltered feed cache")
    st.caption("Everything polled this cycle, for reference — relevance is decided above.")

    forum = database.recent_forum_posts(limit=8)
    with st.expander(f"Forum RSS · {len(forum)} cached posts"):
        if not forum:
            st.caption("No forum posts cached yet.")
        for post in forum:
            st.markdown(
                f"**{post.get('title') or 'Untitled'}** · {post.get('feed') or ''} "
                f"· {(post.get('published') or '')[:10]}"
            )
            if post.get("summary"):
                st.caption(post["summary"][:320])
            if post.get("link"):
                st.caption(post["link"])

    videos = database.recent_videos(limit=4)
    with st.expander(f"YouTube · {len(videos)} cached videos"):
        if not videos:
            st.caption("No creator videos cached yet.")
        for video in videos:
            st.markdown(
                f"**{video.get('title') or 'Untitled'}** · {video.get('channel') or ''} "
                f"· {(video.get('published') or '')[:10]}"
            )
            if video.get("link"):
                st.caption(video["link"])
            if video.get("transcript"):
                st.caption((video["transcript"] or "")[:400] + "…")

    reddit = database.recent_reddit_posts(limit=8)
    with st.expander(f"Reddit · {len(reddit)} cached posts"):
        if not reddit:
            st.caption("No Reddit posts cached yet.")
        for post in reddit:
            st.markdown(
                f"**{post.get('title') or 'Untitled'}** · score {post.get('score', 0)} "
                f"· {(post.get('published') or '')[:10]}"
            )
            if post.get("body"):
                st.caption(post["body"][:320])

    wiki = database.recent_wiki_changes(limit=10)
    with st.expander(f"Wiki · {len(wiki)} recent revisions"):
        if not wiki:
            st.caption("No wiki changes cached yet.")
        for change in wiki:
            st.markdown(
                f"**{change.get('title') or 'Untitled'}** · {change.get('user') or ''} "
                f"· {(change.get('ts') or '')[:10]}"
            )
            if change.get("comment"):
                st.caption(change["comment"][:200])

    auction = database.recent_auction_stats(limit=8)
    with st.expander(f"Auction House LBIN · {len(auction)} items"):
        if not auction:
            st.caption("No Coflnet LBIN stats cached yet.")
        for row in auction:
            st.markdown(
                f"**{row.get('tag')}** · median {_coins(row.get('median_price'))} "
                f"· volume {row.get('volume') or 0:,.0f}"
            )
            if row.get("flags"):
                st.caption(f"flags: {row['flags']}")

    mayor = database.current_mayor()
    with st.expander("Mayor / election cycle"):
        if not mayor:
            st.caption("No election snapshot cached yet.")
        else:
            st.markdown(
                f"**{mayor.get('mayor_name') or 'Unknown'}** · election year "
                f"{mayor.get('election_year') or '-'}"
            )
            for perk in mayor.get("perks") or []:
                st.markdown(f"- **{perk.get('name')}**: {perk.get('description')}")
            candidates = mayor.get("candidates") or []
            if candidates:
                st.caption(
                    "Next ballot: "
                    + ", ".join(f"{c.get('name')} ({c.get('votes', 0):,})" for c in candidates)
                )
