"""Cerebras key/model switching plus a local spend ledger.

The dashboard writes the key and the model into SQLite, so the Streamlit
process and the background daemon both pick up a swap on their next request
(no restart). Every completion is priced from the token usage it reports and
deducted from the starting credit, so the sidebar can show what is left.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import config
import database

KEY_SETTING = "cerebras_api_key"
MODEL_SETTING = "cerebras_model"
BUDGET_SETTING = "llm_budget"
DEFAULT_BALANCE_USD = 5.0

# USD per million tokens. Swapping the model changes the burn rate immediately.
MODEL_CATALOG: dict[str, dict[str, Any]] = {
    "gpt-oss-120b": {
        "label": "OpenAI GPT-OSS 120B",
        "input_per_m": 0.35,
        "output_per_m": 0.75,
    },
    "qwen-3-32b": {
        "label": "Qwen3 32B",
        "input_per_m": 0.99,
        "output_per_m": 1.49,
    },
}


def fingerprint(key: str) -> str:
    """Short, non-reversible id so the ledger can detect a key swap."""
    return hashlib.sha256((key or "").encode()).hexdigest()[:12]


def api_key() -> str:
    """Key saved from the dashboard, falling back to env.txt."""
    return database.get_setting(KEY_SETTING, "") or config.CEREBRAS_API_KEY


def model() -> str:
    """Model saved from the dashboard, falling back to env.txt."""
    return database.get_setting(MODEL_SETTING, "") or config.CEREBRAS_MODEL


def rates(model_id: str | None = None) -> tuple[float, float]:
    """(input, output) USD per million tokens for the chosen model."""
    entry = MODEL_CATALOG.get(model_id or model()) or MODEL_CATALOG["gpt-oss-120b"]
    return float(entry["input_per_m"]), float(entry["output_per_m"])


def estimate_tokens(text: str) -> int:
    """Rough token count used only when the API omits usage."""
    return max(len(text or "") // 4, 1)


def _blank(key: str) -> dict[str, Any]:
    return {
        "key_fp": fingerprint(key),
        "starting_usd": DEFAULT_BALANCE_USD,
        "spent_usd": 0.0,
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "reset_at": database.utcnow(),
    }


def state() -> dict[str, Any]:
    """Current ledger; a changed key seeds a fresh $5.00 balance."""
    key = api_key()
    raw = database.get_setting(BUDGET_SETTING, "")
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("key_fp") != fingerprint(key):
        data = _blank(key)
        database.set_setting(BUDGET_SETTING, json.dumps(data))
    return data


def snapshot() -> dict[str, Any]:
    """Ledger plus the derived numbers the sidebar renders."""
    data = state()
    starting = float(data.get("starting_usd") or DEFAULT_BALANCE_USD)
    spent = float(data.get("spent_usd") or 0.0)
    requests = int(data.get("requests") or 0)
    in_rate, out_rate = rates()
    return {
        **data,
        "model": model(),
        "starting_usd": starting,
        "spent_usd": spent,
        "remaining_usd": max(starting - spent, 0.0),
        "pct_used": min(spent / starting, 1.0) if starting > 0 else 1.0,
        "avg_cost_usd": spent / requests if requests else 0.0,
        "input_rate": in_rate,
        "output_rate": out_rate,
    }


def set_api_key(key: str) -> bool:
    """Save a key. A genuinely different key restarts the credit at $5.00."""
    key = (key or "").strip()
    if not key:
        return False
    changed = fingerprint(key) != fingerprint(api_key())
    database.set_setting(KEY_SETTING, key)
    if changed:
        database.set_setting(BUDGET_SETTING, json.dumps(_blank(key)))
    return changed


def set_model(model_id: str) -> None:
    if model_id in MODEL_CATALOG:
        database.set_setting(MODEL_SETTING, model_id)


def add_credit(amount: float) -> dict[str, Any]:
    """Top-up: raise the starting balance, keeping what has already been spent."""
    data = state()
    data["starting_usd"] = float(data.get("starting_usd") or 0.0) + max(float(amount), 0.0)
    database.set_setting(BUDGET_SETTING, json.dumps(data))
    return snapshot()


def record_usage(
    model_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    purpose: str = "",
) -> dict[str, Any]:
    """Price one completed request and deduct it from the balance."""
    prompt = max(int(prompt_tokens or 0), 0)
    completion = max(int(completion_tokens or 0), 0)
    in_rate, out_rate = rates(model_id)
    cost = prompt / 1_000_000 * in_rate + completion / 1_000_000 * out_rate

    data = state()
    data["spent_usd"] = float(data.get("spent_usd") or 0.0) + cost
    data["requests"] = int(data.get("requests") or 0) + 1
    data["input_tokens"] = int(data.get("input_tokens") or 0) + prompt
    data["output_tokens"] = int(data.get("output_tokens") or 0) + completion
    database.set_setting(BUDGET_SETTING, json.dumps(data))
    database.log_llm_usage(
        model=model_id or "unknown",
        purpose=purpose,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cost_usd=cost,
        key_fp=str(data.get("key_fp") or ""),
    )
    return {"cost_usd": cost, **data}
