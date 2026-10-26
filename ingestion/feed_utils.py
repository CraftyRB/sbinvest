"""Shared helpers for RSS/Atom feeds."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def clean_html(raw: str | None, limit: int = 2000) -> str:
    text = html.unescape(_TAG.sub(" ", raw or ""))
    return _SPACE.sub(" ", text).strip()[:limit]


def published_iso(entry: dict) -> str:
    """Best-effort ISO timestamp from a feedparser entry."""
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc).isoformat(timespec="seconds")
            except (TypeError, ValueError):
                continue
    return entry.get("published") or entry.get("updated") or ""
