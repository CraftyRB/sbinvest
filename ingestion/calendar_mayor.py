"""Hypixel mayor / election context.

The /v2/resources/skyblock/election payload contains:
  mayor   -> the sitting mayor and perks
  current -> the next election (year, candidates, votes, minister perks)
"""

from __future__ import annotations

from datetime import datetime, timezone

import requests

import config
import database


class MayorError(RuntimeError):
    pass


def fetch_election() -> dict:
    headers = {"User-Agent": "skyblock-ai-dashboard/1.0"}
    if config.HYPIXEL_API_KEY:
        headers["API-Key"] = config.HYPIXEL_API_KEY
    try:
        resp = requests.get(
            config.HYPIXEL_ELECTION_URL,
            headers=headers,
            timeout=config.SOURCE_REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise MayorError(f"Election request failed: {exc}") from exc
    if resp.status_code != 200:
        raise MayorError(f"Election returned HTTP {resp.status_code}")
    payload = resp.json()
    if not payload.get("success", True):
        raise MayorError(f"Election error: {payload.get('cause', 'unknown')}")
    return payload


def normalise(payload: dict) -> dict:
    mayor = payload.get("mayor") or {}
    election = payload.get("current") or {}
    timestamp = payload.get("lastUpdated")
    ts = (
        datetime.fromtimestamp(float(timestamp) / 1000, tz=timezone.utc).isoformat(timespec="seconds")
        if timestamp
        else database.utcnow()
    )
    return {
        "ts": ts,
        "mayor_key": mayor.get("key"),
        "mayor_name": mayor.get("name"),
        "perks": [
            {"name": p.get("name"), "description": p.get("description")}
            for p in (mayor.get("perks") or [])
        ],
        "election_year": election.get("year"),
        "candidates": [
            {"name": c.get("name"), "key": c.get("key"), "votes": c.get("votes")}
            for c in (election.get("candidates") or [])
        ],
    }


def collect_mayor_context() -> dict:
    """Return {'snapshot': dict|None, 'errors': [str]} and persist the snapshot."""
    try:
        snapshot = normalise(fetch_election())
    except Exception as exc:
        return {"snapshot": None, "errors": [str(exc)]}
    database.save_mayor_cycle(snapshot)
    return {"snapshot": snapshot, "errors": []}
