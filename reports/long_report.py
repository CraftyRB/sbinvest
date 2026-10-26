"""Long-term investment due-diligence report.

Every catalyst-driven (LONG) call gets a written explanation: which update it
depends on, why that update moves supply or demand, what evidence backs it, the
expected return and date, the risks the bear case raised, and the exit plan.
The output is a PDF for reading plus a markdown copy for quick viewing, both
written to `reports/output/` and referenced from the dashboard.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OUTPUT_DIR = Path(__file__).resolve().parent / "output"


def _clean(text: Any, limit: int = 2000) -> str:
    """Collapse whitespace and force Latin-1 so the core PDF fonts can render."""
    raw = " ".join(str(text or "").split())
    for src, dst in (("\u2014", "-"), ("\u2013", "-"), ("\u2019", "'"), ("\u201c", '"'), ("\u201d", '"')):
        raw = raw.replace(src, dst)
    return raw.encode("latin-1", "replace").decode("latin-1")[:limit]


def _money(value: Any) -> str:
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return "-"


def _pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "-"


def _price(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"


def _ordered(signals: list[dict]) -> list[dict]:
    order = {"BUY": 0, "HOLD": 1, "SELL": 2}
    return sorted(
        signals,
        key=lambda s: (
            order.get(str(s.get("signal") or "").upper(), 3),
            -float(s.get("confidence") or 0),
        ),
    )


def _catalyst_lines(signal: dict, upcoming: list[dict]) -> list[str]:
    lines = [str(c) for c in (signal.get("catalysts") or []) if str(c).strip()]
    if signal.get("catalyst_date"):
        lines.append(f"Catalyst date on record: {signal['catalyst_date']}")
    sector = str(signal.get("sector") or "").upper()
    for event in upcoming:
        sectors = [str(s).upper() for s in (event.get("sectors") or [])]
        if sector and sector not in sectors:
            continue
        lines.append(
            f"{event.get('title')} - expected {event.get('expected_date') or 'date unconfirmed'} "
            f"(confidence {_pct(event.get('confidence'))}, status {event.get('status') or 'predicted'})"
        )
        if event.get("notes"):
            lines.append(_clean(event["notes"], 400))
    # De-duplicate while keeping order.
    seen: set[str] = set()
    unique = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            unique.append(line)
    return unique[:6]


def _markdown(signals: list[dict], upcoming: list[dict], calibration: dict, generated: str) -> str:
    out: list[str] = []
    out.append("# SkyBlock Long-Term Investment Report")
    out.append("")
    out.append(f"Generated: {generated}")
    out.append("")
    if calibration:
        out.append(
            f"Track record: {calibration.get('n', 0)} graded calls, "
            f"weighted hit rate {_pct(calibration.get('hit_rate'))}."
        )
        out.append("")

    if upcoming:
        out.append("## Update calendar")
        for event in upcoming[:10]:
            out.append(
                f"- **{_clean(event.get('title'), 120)}** - expected "
                f"{event.get('expected_date') or 'date unconfirmed'}, "
                f"confidence {_pct(event.get('confidence'))}, "
                f"sectors {', '.join(event.get('sectors') or []) or '-'}"
            )
        out.append("")

    out.append("## Positions")
    if not signals:
        out.append("_No catalyst-mapped long ideas were issued this cycle._")
    for signal in _ordered(signals):
        name = _clean(signal.get("item_name") or signal.get("item_id"), 80)
        out.append(f"### {name} - {str(signal.get('signal') or '').upper()}")
        out.append("")
        out.append(
            f"- Sector: {signal.get('sector') or '-'} | risk tier: {signal.get('risk_tier') or '-'}"
        )
        out.append(
            f"- Confidence: {_pct(signal.get('confidence'))} | confluence: "
            f"{signal.get('confluence_score') or '-'}"
        )
        out.append(
            f"- Entry: {_price(signal.get('entry_price'))} | target: "
            f"{_price(signal.get('target_price'))} | stop: {_price(signal.get('stop_price'))}"
        )
        out.append(
            f"- Expected return: {_pct((signal.get('expected_return_pct') or 0) / 100)} "
            f"over about {signal.get('expected_days_to_target') or '-'} days "
            f"(around {signal.get('expected_target_date') or '-'})."
        )
        out.append(f"- Forecast method: {signal.get('forecast_method') or '-'}")
        if signal.get("thesis"):
            out.append(f"- Thesis: {_clean(signal.get('thesis'), 900)}")
        catalysts = _catalyst_lines(signal, upcoming)
        if catalysts:
            out.append("- Catalyst:")
            for line in catalysts:
                out.append(f"  - {_clean(line, 300)}")
        if signal.get("bull_case"):
            out.append(f"- Bull case: {_clean(signal.get('bull_case'), 600)}")
        if signal.get("bear_case"):
            out.append(f"- Risks / bear case: {_clean(signal.get('bear_case'), 600)}")
        if signal.get("sources"):
            out.append("- Evidence used:")
            for source in (signal.get("sources") or [])[:8]:
                out.append(f"  - {_clean(source, 240)}")
        out.append(
            f"- Exit plan: sell at the target for roughly "
            f"{_money((signal.get('target_price') or 0) - (signal.get('entry_price') or 0))} "
            f"profit per unit, or cut at the stop; re-evaluate if the catalyst slips past "
            f"{signal.get('catalyst_date') or signal.get('expected_target_date') or 'the forecast date'}."
        )
        out.append("")
    out.append("---")
    out.append("")
    out.append(
        "This report is generated by the desk's own analysis. It grades itself after the "
        "forecast window closes, whether or not a trade was placed, and the results adjust "
        "future confidence and target dates."
    )
    return "\n".join(out)


def _pdf(
    path: Path,
    signals: list[dict],
    upcoming: list[dict],
    calibration: dict,
    generated: str,
) -> None:
    from fpdf import FPDF

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    def line(text: str, height: float = 4.5) -> None:
        # multi_cell leaves the cursor at the right edge unless told otherwise,
        # and the next width-0 cell then has no room.
        pdf.multi_cell(0, height, text, new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "B", 18)
    line("SkyBlock Long-Term Investment Report", 9)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(90, 90, 90)
    line(f"Generated: {generated}", 5)
    if calibration:
        line(
            f"Track record: {calibration.get('n', 0)} graded calls | "
            f"weighted hit rate {_pct(calibration.get('hit_rate'))}",
            5,
        )
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    if upcoming:
        pdf.set_font("Helvetica", "B", 12)
        line("Update calendar", 6)
        pdf.set_font("Helvetica", "", 8)
        for event in upcoming[:10]:
            line(
                "- " + _clean(event.get("title"), 110) + " | expected "
                + str(event.get("expected_date") or "date unconfirmed")
                + " | confidence " + _pct(event.get("confidence"))
                + " | sectors " + ", ".join(event.get("sectors") or []),
            )
        pdf.ln(2)

    pdf.set_font("Helvetica", "B", 12)
    line("Positions", 6)
    if not signals:
        pdf.set_font("Helvetica", "I", 9)
        line("No catalyst-mapped long ideas were issued this cycle.", 5)

    for signal in _ordered(signals):
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 11)
        line(
            _clean(signal.get("item_name") or signal.get("item_id"), 80)
            + "  [" + str(signal.get("signal") or "").upper() + "]",
            5.5,
        )
        pdf.set_font("Helvetica", "", 8)
        line(
            f"Sector {signal.get('sector') or '-'} | tier {signal.get('risk_tier') or '-'} | "
            f"confidence {_pct(signal.get('confidence'))} | confluence "
            f"{signal.get('confluence_score') or '-'}",
        )
        line(
            f"Entry {_price(signal.get('entry_price'))} | target "
            f"{_price(signal.get('target_price'))} | stop {_price(signal.get('stop_price'))}",
        )
        line(
            f"Expected return {_pct((signal.get('expected_return_pct') or 0) / 100)} in about "
            f"{signal.get('expected_days_to_target') or '-'} days "
            f"(around {signal.get('expected_target_date') or '-'}) via "
            f"{signal.get('forecast_method') or '-'}.",
        )
        if signal.get("thesis"):
            line("Thesis: " + _clean(signal.get("thesis"), 900))
        catalysts = _catalyst_lines(signal, upcoming)
        if catalysts:
            line("Catalyst:")
            for cat in catalysts:
                line("  - " + _clean(cat, 280), 4.2)
        if signal.get("bull_case"):
            line("Bull case: " + _clean(signal.get("bull_case"), 600))
        if signal.get("bear_case"):
            line("Risks: " + _clean(signal.get("bear_case"), 600))
        if signal.get("sources"):
            line("Evidence used:")
            for source in (signal.get("sources") or [])[:8]:
                line("  - " + _clean(source, 220), 4.2)
        line(
            "Exit plan: take profit at the target, cut at the stop, and re-evaluate if the "
            "catalyst slips past "
            + str(signal.get("catalyst_date") or signal.get("expected_target_date") or "the forecast date")
            + ".",
        )

    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 7.5)
    pdf.set_text_color(100, 100, 100)
    line(
        "Generated by the SkyBlock investment desk. Every call is graded after its forecast "
        "window closes, whether or not a trade was placed; the results adjust future confidence "
        "and target dates.",
        4,
    )
    pdf.output(str(path))


def build_long_report(
    signals: list[dict],
    upcoming: list[dict] | None = None,
    calibration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the PDF and markdown report; returns their paths and a summary."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    generated = now.strftime("%Y-%m-%d %H:%M UTC")
    signals = signals or []
    upcoming = upcoming or []
    calibration = calibration or {}

    markdown_path = OUTPUT_DIR / f"long_term_{stamp}.md"
    pdf_path = OUTPUT_DIR / f"long_term_{stamp}.pdf"
    markdown_path.write_text(
        _markdown(signals, upcoming, calibration, generated), encoding="utf-8"
    )
    _pdf(pdf_path, signals, upcoming, calibration, generated)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "pdf": str(pdf_path),
        "markdown": str(markdown_path),
        "signals": len(signals),
        "buys": sum(1 for s in signals if str(s.get("signal") or "").upper() == "BUY"),
    }
