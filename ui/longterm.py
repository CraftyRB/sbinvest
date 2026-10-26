"""Long-Term Research tab: update calendar, catalyst positions, report and grades."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

import database


def _pct(value: float | None) -> str:
    try:
        return f"{float(value):.0%}"
    except (TypeError, ValueError):
        return "-"


def _coins(value: float | None) -> str:
    if value is None:
        return "-"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,.0f}"


def _loads(raw: str | None, fallback):
    try:
        return json.loads(raw or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _calendar() -> tuple[dict, list[dict]]:
    blob = _loads(database.get_setting("last_update_calendar"), {})
    events = blob.get("events") or database.list_update_calendar(limit=40)
    return blob, events


def _report() -> dict:
    return _loads(database.get_setting("last_long_report"), {})


def _render_calendar(blob: dict, events: list[dict]) -> None:
    st.markdown("#### Update calendar")
    st.caption(
        "Announced, predicted and leaked game updates mapped to the sectors they move. "
        "Long positions are only opened when one of these touches the item or its sector."
    )
    if blob.get("generated_at"):
        st.caption(
            f"Extracted {blob['generated_at'][:16].replace('T', ' ')} · "
            f"channels {', '.join(blob.get('sectors') or []) or '-'} · "
            f"mode `{blob.get('mode') or '-'}`"
        )
    if not events:
        st.info(
            "No update events extracted yet. Run a scan or press 'Update evidence resources' "
            "in the sidebar so the forum, wiki and commit feeds can be mined for update news."
        )
        return
    rows = [
        {
            "Update": e.get("title") or "-",
            "Expected": e.get("expected_date") or "date unconfirmed",
            "Window": f"{e.get('window_days') or '-'}d",
            "Confidence": _pct(e.get("confidence")),
            "Status": e.get("status") or "-",
            "Sectors": ", ".join(e.get("sectors") or []) or "-",
            "Items": ", ".join((e.get("items") or [])[:4]) or "-",
            "Notes": (e.get("notes") or "")[:160],
        }
        for e in events
    ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=260)


def _render_calls(signals: list[dict]) -> None:
    st.markdown("#### Catalyst positions")
    st.caption(
        "LONG calls are 1-4 week positions. The expected return date blends the item's own "
        "volatility with the catalyst date, and is re-learned from past forecast accuracy."
    )
    if not signals:
        st.info(
            "No long-term calls are open. A LONG call needs a named update catalyst, so this "
            "stays empty when the calendar has nothing that maps to a liquid item."
        )
        return
    params = _loads(database.get_setting("last_calibration"), {})
    rows = [
        {
            "Item": s["item_id"],
            "Signal": s["signal"],
            "Conf": _pct(s.get("confidence")),
            "Sector": s.get("sector") or "-",
            "Entry": _coins(s.get("entry_price")),
            "Target": _coins(s.get("target_price")),
            "Stop": _coins(s.get("stop_price")),
            "Expected return": f"{float(s.get('expected_return_pct') or 0):+.1f}%",
            "Expected date": s.get("expected_target_date") or "-",
            "Days": s.get("expected_days_to_target") or "-",
            "Catalyst date": s.get("catalyst_date") or "-",
            "Status": s.get("status") or "-",
            "P&L": f"{s['pnl_pct']:+.2f}%" if s.get("pnl_pct") is not None else "-",
        }
        for s in signals
    ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=300)

    st.markdown("##### Why each position exists")
    for s in signals:
        label = (
            f"{s['item_id']} · {s['signal']} · conf {_pct(s.get('confidence'))} · "
            f"target {_coins(s.get('target_price'))} by {s.get('expected_target_date') or '-'}"
        )
        with st.expander(label):
            if s.get("catalysts"):
                st.markdown("**Catalyst**")
                for catalyst in s["catalysts"]:
                    st.markdown(f"- {catalyst}")
            if s.get("thesis"):
                st.markdown("**Thesis**")
                st.write(s["thesis"])
            if s.get("bear_case"):
                st.markdown("**Risks (bear case)**")
                st.write(s["bear_case"])
            if s.get("sources"):
                st.markdown("**Evidence used**")
                for source in s["sources"]:
                    st.markdown(f"- {source}")
            st.caption(
                f"Forecast method: `{s.get('forecast_method') or '-'}` · horizon "
                f"{s.get('horizon_days') or '-'}d · grade "
                f"{s.get('grade') or 'pending'}"
            )
    if params:
        st.caption(
            f"Calibration: {params.get('n', 0)} graded predictions · weighted hit rate "
            f"{_pct(params.get('hit_rate'))}."
        )


def _render_report(long_signals: list[dict], events: list[dict]) -> None:
    st.markdown("#### Written report")
    st.caption(
        "A PDF and markdown dossier explaining every long call: the update behind it, the "
        "evidence trail, the expected return window, the risks and the exit plan."
    )
    report = _report()
    if st.button("Generate report now", use_container_width=False):
        try:
            from reports.long_report import build_long_report

            calibration = _loads(database.get_setting("last_calibration"), {})
            report = build_long_report(long_signals, events, calibration)
            database.set_setting("last_long_report", json.dumps(report, default=str))
            st.success("Report generated.")
        except Exception as exc:  # noqa: BLE001 - surface any renderer failure
            st.error(f"Report generation failed: {exc}")

    if not report:
        st.info("No report yet. It is generated automatically after a scan with long calls.")
        return
    st.caption(
        f"Generated {report.get('generated_at', '')[:16].replace('T', ' ')} · "
        f"{report.get('signals', 0)} positions ({report.get('buys', 0)} BUY)"
    )
    pdf = Path(report.get("pdf") or "")
    markdown = Path(report.get("markdown") or "")
    cols = st.columns(2)
    if pdf.is_file():
        cols[0].download_button(
            "Download PDF report",
            data=pdf.read_bytes(),
            file_name=pdf.name,
            mime="application/pdf",
            use_container_width=True,
        )
    if markdown.is_file():
        cols[1].download_button(
            "Download markdown",
            data=markdown.read_text(encoding="utf-8").encode("utf-8"),
            file_name=markdown.name,
            mime="text/markdown",
            use_container_width=True,
        )
    if markdown.is_file():
        with st.expander("Preview report", expanded=False):
            st.markdown(markdown.read_text(encoding="utf-8"))


def _render_grades() -> None:
    calibration = _loads(database.get_setting("last_calibration"), {})
    st.markdown("#### Self-learning record")
    st.caption(
        "Every BUY/SELL prediction is graded at its forecast horizon whether or not a trade "
        "was placed. Grades adjust the confidence multiplier and the target-date scale used "
        "on the next scan."
    )
    if calibration:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Graded calls", calibration.get("n", 0))
        c2.metric("Weighted hit rate", _pct(calibration.get("hit_rate")))
        c3.metric("Avg accuracy", _pct(calibration.get("avg_accuracy")))
        short = (calibration.get("by_term") or {}).get("SHORT") or {}
        long = (calibration.get("by_term") or {}).get("LONG") or {}
        c4.metric(
            "Terms",
            f"S {short.get('n', 0)} / L {long.get('n', 0)}",
            help="Graded short-term vs long-term predictions",
        )

    grades = database.list_prediction_grades(limit=40)
    if not grades:
        st.caption("Nothing graded yet. The first predictions are graded once their horizon closes.")
        return
    rows = [
        {
            "Graded": (g.get("graded_at") or "")[:16].replace("T", " "),
            "Item": g.get("item_id"),
            "Term": g.get("term"),
            "Signal": g.get("signal"),
            "Conf": _pct(g.get("confidence")),
            "Predicted days": g.get("predicted_days"),
            "Actual days": g.get("actual_days"),
            "Predicted return": f"{float(g.get('predicted_return_pct') or 0):+.1f}%",
            "Actual return": f"{float(g.get('actual_return_pct') or 0):+.1f}%",
            "Grade": g.get("grade"),
            "Note": (g.get("note") or "")[:120],
        }
        for g in grades
    ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=280)


def render(last_scan: dict | None) -> None:
    st.subheader("Long-Term Research")
    st.caption(
        "Catalyst-driven positions: what is changing in the game, which items that update "
        "touches, when the move is expected, and the written case for each call."
    )

    if last_scan and last_scan.get("error"):
        st.error(f"Last scan failed: {last_scan['error']}")
    if last_scan and not last_scan.get("error"):
        long_view = last_scan.get("market_view_long")
        if long_view:
            st.info(long_view)
        upcoming = last_scan.get("upcoming_updates") or []
        st.caption(
            f"Last scan {str(last_scan.get('scanned_at', ''))[:16].replace('T', ' ')} · "
            f"{len(upcoming)} upcoming updates · "
            f"{len(last_scan.get('long_signals') or [])} long calls"
        )

    blob, events = _calendar()
    long_signals = database.list_signals(status="open", limit=100, term="LONG")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Upcoming updates", len(events))
    c2.metric("Open long calls", len(long_signals))
    c3.metric("Catalyst sectors", len(blob.get("sectors") or []))
    calibration = _loads(database.get_setting("last_calibration"), {})
    c4.metric("Graded predictions", calibration.get("n", 0))

    _render_calendar(blob, events)
    st.divider()
    _render_calls(long_signals)
    st.divider()
    _render_report(long_signals, events)
    st.divider()
    _render_grades()
