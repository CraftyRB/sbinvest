"""Pre-search research planner.

Acceptance of scraped evidence was low because the searcher and the relevance
gate had different ideas about what mattered. This module asks Cerebras,
*before* any search runs, three things per ticker:

  1. what questions would actually settle the trade,
  2. which queries should be used to find that evidence,
  3. exactly what it will accept or reject when judging the results.

Those criteria are then injected into both the search and the evidence gate, so
the gate is grading against the same rubric that produced the queries.
"""

from __future__ import annotations

import json
from typing import Any

import config
from analytics import sector_for
from intelligence import client

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "tickers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "why_watch": {"type": "string"},
                    "questions": {"type": "array", "items": {"type": "string"}},
                    "queries": {"type": "array", "items": {"type": "string"}},
                    "accept_if": {"type": "array", "items": {"type": "string"}},
                    "reject_if": {"type": "array", "items": {"type": "string"}},
                    "catalyst": {"type": "string"},
                },
                "required": [
                    "ticker", "why_watch", "questions", "queries",
                    "accept_if", "reject_if", "catalyst",
                ],
                "additionalProperties": False,
            },
        },
        "global_queries": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "tickers", "global_queries"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are the research director on a SkyBlock Bazaar desk. Before any web search runs, "
    "design the evidence plan for each ticker. The evidence auditor will use YOUR accept_if "
    "and reject_if rules, so write them as concrete, checkable criteria - not vague wishes. "
    "For each ticker give: why_watch (the trade idea in one sentence), questions (2-3 short "
    "questions that must be answered to confirm or kill it), queries (2-3 short search strings "
    "that would find that answer; include item name plus 'recipe', 'drop', 'update', 'price', "
    "'farm' as appropriate), accept_if (specific things that count as relevant: named item, its recipe, "
    "a mechanic that consumes it, a sector update that changes its supply/demand, patch note, "
    "leak), reject_if (chatter that must be dropped: memes, showcases, unrelated builds, "
    "party requests, old news with no price mechanism), catalyst (the update or event that "
    "would trigger the move, or empty string if none). Also return 3-5 global_queries for "
    "sector-wide update news. Do not invent item ids or facts. "
    "STYLE: SHORT means a multi-day dip/flip - focus on near-term demand, market saturation, "
    "flippers and patch-day flows. LONG means a 1-4 week catalyst trade - focus on upcoming "
    "game updates, recipe changes, new islands, skill revamps, leaks and creator coverage. "
    "OUTPUT: return only the JSON object described by the schema - no markdown, no headings, "
    "no commentary, no trailing prose."
)


def _compact(candidates: list[dict], updates: list[dict] | None) -> list[dict]:
    return [
        {
            "ticker": c["item_id"],
            "name": c.get("item_name"),
            "sector": sector_for(c["item_id"]),
            "mid_price": c.get("mid_price"),
            "weekly_volume_coins": c.get("weekly_volume_coins"),
            "price_vs_ma7": c.get("risk_ratio"),
            "dip_z": c.get("dip_z"),
            "volatility_pct": c.get("volatility_pct"),
            "momentum_pct": c.get("momentum_pct"),
        }
        for c in candidates
    ]


def _display(candidate: dict) -> str:
    name = (candidate.get("item_name") or "").strip()
    return name or candidate["item_id"].replace("_", " ").title()


def _fallback(candidates: list[dict], term: str) -> dict[str, Any]:
    tickers = []
    for c in candidates:
        name = _display(c)
        sector = sector_for(c["item_id"])
        tickers.append(
            {
                "ticker": c["item_id"],
                "why_watch": f"{name} is a {sector.lower()} item being screened for a {term.lower()}-term trade.",
                "questions": [
                    f"Did anything change in {name}'s recipe, drop table or requirement?",
                    f"Is {name} consumed by current or upcoming {sector.lower()} progression?",
                    f"What is the current supply and weekly demand for {name}?",
                ],
                "queries": [
                    f'"{name}" SkyBlock recipe change',
                    f'"{name}" SkyBlock update',
                    f"{name} SkyBlock price",
                ],
                "accept_if": [
                    f"Mentions {name} or its recipe",
                    f"{sector} update that changes supply or demand for {name}",
                    "Patch note, alpha leak or creator coverage naming the item or its mechanic",
                ],
                "reject_if": [
                    "Memes, showcases, unrelated builds and party requests",
                    "Price talk with no supply, demand or recipe mechanism",
                ],
                "catalyst": "",
            }
        )
    return {
        "summary": f"Deterministic {term.lower()}-term research plan.",
        "tickers": tickers,
        "global_queries": [f"Hypixel SkyBlock {term.lower()} term update news"],
        "mode": "heuristic",
        "term": term.upper(),
    }


def _index(plan: dict[str, Any]) -> dict[str, Any]:
    criteria: dict[str, dict] = {}
    queries: dict[str, list[str]] = {}
    for row in plan.get("tickers") or []:
        ticker = str(row.get("ticker", "")).upper()
        if not ticker:
            continue
        criteria[ticker] = {
            "why_watch": str(row.get("why_watch") or "")[:300],
            "questions": [str(q)[:200] for q in (row.get("questions") or [])][:5],
            "accept_if": [str(q)[:200] for q in (row.get("accept_if") or [])][:6],
            "reject_if": [str(q)[:200] for q in (row.get("reject_if") or [])][:5],
            "catalyst": str(row.get("catalyst") or "")[:200],
        }
        cleaned = []
        for query in row.get("queries") or []:
            text = str(query).strip()[:120]
            if text and text not in cleaned:
                cleaned.append(text)
        if cleaned:
            queries[ticker] = cleaned[: config.PLANNER_QUERIES_PER_ITEM]
    return {
        "criteria": criteria,
        "queries": queries,
        "global_queries": [str(q)[:120] for q in (plan.get("global_queries") or [])][:5],
        "summary": str(plan.get("summary") or "")[:500],
    }


def plan_research(
    candidates: list[dict],
    term: str = "SHORT",
    calibration: dict[str, Any] | None = None,
    updates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return {criteria, queries, global_queries, summary, mode, term}."""
    term = (term or "SHORT").upper()
    if not candidates:
        return {**_index({"tickers": [], "global_queries": []}), "mode": "empty", "term": term}

    if not config.RESEARCH_PLANNER_ENABLED:
        plan = _fallback(candidates, term)
        return {**_index(plan), "mode": "disabled", "term": term}

    payload = {
        "term": term,
        "objective": (
            "multi-day dip/flip; time horizon 1-3 days"
            if term == "SHORT"
            else "catalyst-driven position; time horizon 1-4 weeks"
        ),
        "track_record": {
            "hit_rate": (calibration or {}).get("hit_rate"),
            "by_term": ((calibration or {}).get("by_term") or {}).get(term),
            "by_confidence_bucket": (calibration or {}).get("by_bucket"),
            "note": "If recent hit rate is low, demand stronger and more specific evidence.",
        },
        "upcoming_updates": [
            {
                "title": u.get("title"),
                "expected_date": u.get("expected_date"),
                "confidence": u.get("confidence"),
                "sectors": u.get("sectors"),
                "notes": (u.get("notes") or "")[:200],
            }
            for u in (updates or [])[:8]
        ],
        "candidates": _compact(candidates, updates),
    }

    try:
        result = client.chat(
            SYSTEM,
            "Design the evidence plan for these tickers.\n\n"
            + json.dumps(payload, separators=(",", ":")),
            purpose=f"research-plan:{term.lower()}",
            temperature=0.2,
            max_tokens=8000,
            schema=PLAN_SCHEMA,
            schema_name="research_plan",
        )
        plan = result.data
        mode = result.mode
    except Exception as exc:
        plan = _fallback(candidates, term)
        mode = f"heuristic:{type(exc).__name__}"

    indexed = _index(plan)
    indexed["mode"] = mode
    indexed["term"] = term
    indexed["raw"] = plan
    return indexed
