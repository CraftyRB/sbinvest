"""SQLite persistence for market snapshots, signals, leaks, portfolios and the
self-improving feedback loop.

The database is the single source of truth for:
  * price history (used to compute the 7-day moving average)
  * every AI signal plus its realised outcome (won / lost / open)
  * early-leak commits already processed by the intelligence layer
  * user portfolios and holdings
  * model call telemetry
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     TEXT NOT NULL,
    ts          TEXT NOT NULL,
    buy_price   REAL NOT NULL,
    sell_price  REAL NOT NULL,
    mid_price   REAL NOT NULL,
    buy_volume  REAL NOT NULL DEFAULT 0,
    sell_volume REAL NOT NULL DEFAULT 0,
    UNIQUE(item_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_item_ts ON market_snapshots(item_id, ts);

CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    item_id       TEXT NOT NULL,
    item_name     TEXT NOT NULL,
    signal        TEXT NOT NULL,
    confidence    REAL NOT NULL,
    entry_price   REAL,
    target_price  REAL,
    stop_price    REAL,
    risk_ratio    REAL,
    ma7           REAL,
    sector        TEXT,
    horizon_days  INTEGER,
    catalysts     TEXT,
    thesis        TEXT,
    source        TEXT,
    commit_sha    TEXT,
    sources       TEXT,
    confluence_score REAL,
    risk_tier     TEXT,
    bull_case     TEXT,
    bear_case     TEXT,
    status        TEXT NOT NULL DEFAULT 'open',
    exit_price    REAL,
    pnl_pct       REAL,
    resolved_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status, created_at);

CREATE TABLE IF NOT EXISTS leaks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sha         TEXT NOT NULL UNIQUE,
    ts          TEXT,
    title       TEXT,
    author      TEXT,
    files       TEXT,
    diff        TEXT,
    summary     TEXT,
    items       TEXT,
    seen        INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolios (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    purse       REAL NOT NULL DEFAULT 0,
    cash        REAL NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS holdings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    item_id       TEXT NOT NULL,
    qty           REAL NOT NULL,
    cost_basis    REAL NOT NULL,
    opened_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    purpose     TEXT NOT NULL,
    model       TEXT NOT NULL,
    latency_ms  INTEGER,
    ok          INTEGER NOT NULL,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS forum_posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT NOT NULL UNIQUE,
    title       TEXT,
    link        TEXT,
    published   TEXT,
    author      TEXT,
    summary     TEXT,
    feed        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS youtube_videos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id    TEXT NOT NULL UNIQUE,
    title       TEXT,
    channel     TEXT,
    published   TEXT,
    link        TEXT,
    transcript  TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reddit_posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     TEXT NOT NULL UNIQUE,
    title       TEXT,
    author      TEXT,
    published   TEXT,
    link        TEXT,
    body        TEXT,
    score       INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auction_stats (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tag           TEXT NOT NULL,
    ts            TEXT NOT NULL,
    min_price     REAL,
    median_price  REAL,
    mean_price    REAL,
    mode_price    REAL,
    max_price     REAL,
    volume        REAL,
    flags         TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE(tag, ts)
);
CREATE INDEX IF NOT EXISTS idx_auction_tag_ts ON auction_stats(tag, ts);

CREATE TABLE IF NOT EXISTS mayor_cycles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    mayor_key     TEXT,
    mayor_name    TEXT,
    perks         TEXT,
    election_year INTEGER,
    candidates    TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wiki_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    revid       TEXT NOT NULL UNIQUE,
    title       TEXT,
    user        TEXT,
    comment     TEXT,
    ts          TEXT,
    wiki        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS web_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id   TEXT NOT NULL UNIQUE,
    title       TEXT,
    url         TEXT,
    snippet     TEXT,
    query       TEXT,
    published   TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prediction_grades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id           INTEGER,
    term                TEXT NOT NULL DEFAULT 'SHORT',
    item_id             TEXT NOT NULL,
    signal              TEXT NOT NULL,
    confidence          REAL,
    created_at          TEXT NOT NULL,
    graded_at           TEXT NOT NULL,
    horizon_end         TEXT,
    predicted_days      INTEGER,
    actual_days         REAL,
    predicted_target    REAL,
    predicted_return_pct REAL,
    actual_price        REAL,
    actual_return_pct   REAL,
    grade               TEXT NOT NULL,
    accuracy            REAL NOT NULL,
    note                TEXT
);
CREATE INDEX IF NOT EXISTS idx_grades_signal ON prediction_grades(signal_id);
CREATE INDEX IF NOT EXISTS idx_grades_term ON prediction_grades(term, graded_at);

CREATE TABLE IF NOT EXISTS update_calendar (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    expected_date TEXT,
    window_days   INTEGER DEFAULT 30,
    confidence    REAL DEFAULT 0.5,
    status        TEXT DEFAULT 'announced',
    source        TEXT,
    url           TEXT,
    sectors       TEXT,
    items         TEXT,
    notes         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calendar_date ON update_calendar(expected_date);

CREATE TABLE IF NOT EXISTS research_plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    term        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_term ON research_plans(term, created_at);

CREATE TABLE IF NOT EXISTS exit_orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    holding_id   INTEGER NOT NULL,
    item_id      TEXT NOT NULL,
    qty          REAL NOT NULL,
    target_price REAL NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    hit_at       TEXT,
    hit_price    REAL
);
CREATE INDEX IF NOT EXISTS idx_exit_orders_status ON exit_orders(status, item_id);

CREATE TABLE IF NOT EXISTS cashouts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    holding_id   INTEGER,
    item_id      TEXT NOT NULL,
    qty          REAL NOT NULL,
    price        REAL NOT NULL,
    cost_basis   REAL NOT NULL,
    pnl          REAL NOT NULL,
    pnl_pct      REAL NOT NULL,
    sold_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cashouts_portfolio ON cashouts(portfolio_id, sold_at);

CREATE TABLE IF NOT EXISTS llm_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    model             TEXT NOT NULL,
    purpose           TEXT,
    prompt_tokens     INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    cost_usd          REAL NOT NULL,
    key_fp            TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_ISO_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")


def _stamp_cutoff(days: int) -> str:
    """ISO-8601 UTC string exactly `days` days ago, for lexical `ts >=` filters."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _normalize_ts(value: Any) -> str | None:
    """Normalise a feed timestamp to offset-aware ISO for recency comparison.

    Handles MediaWiki 'Z' timestamps, naive ISO (assumed UTC), epoch seconds and
    date-only day strings; returns None when unparseable.
    """
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text[:10].isdigit():
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        if len(text) >= 10 and _ISO_STAMP_RE.match(text):
            return text[:10]
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            return text[:10]
    return None


def _fresh(rows: list[dict[str, Any]], ts_key: str, days: int | None) -> list[dict[str, Any]]:
    """Drop rows whose `ts_key` is older than `days`; keep rows with unparseable stamps."""
    if not days:
        return rows
    cutoff = _stamp_cutoff(int(days))
    kept = []
    for row in rows:
        stamp = _normalize_ts(row.get(ts_key))
        if stamp is None or stamp >= cutoff:
            kept.append(row)
    return kept


@contextmanager
def connection() -> Iterator[Any]:
    if config.TURSO_DATABASE_URL:
        # Cloud deployment: the container has no durable disk, so talk to Turso.
        import turso_backend

        conn: Any = turso_backend.connect(
            config.TURSO_DATABASE_URL, config.TURSO_AUTH_TOKEN
        )
    else:
        conn = sqlite3.connect(config.DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_schema_ready = False


def init_db() -> None:
    global _schema_ready
    # Every Streamlit rerun calls this; on a remote database the DDL would be
    # a burst of round trips, so it runs once per process instead.
    if _schema_ready:
        return
    if not config.TURSO_DATABASE_URL:
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connection() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
    _schema_ready = True


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for databases created by older builds."""
    signal_cols = {r["name"] for r in conn.execute("PRAGMA table_info(signals)")}
    additions = {
        "sources": "TEXT",
        "confluence_score": "REAL",
        "risk_tier": "TEXT",
        "bull_case": "TEXT",
        "bear_case": "TEXT",
        "term": "TEXT DEFAULT 'SHORT'",
        "expected_days_to_target": "INTEGER",
        "expected_target_date": "TEXT",
        "expected_return_pct": "REAL",
        "forecast_method": "TEXT",
        "catalyst_date": "TEXT",
        "close_reason": "TEXT",
        "grade": "TEXT",
        "graded_at": "TEXT",
    }
    for column, sql_type in additions.items():
        if column not in signal_cols:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {column} {sql_type}")


# --------------------------------------------------------------------------- #
# Market snapshots / 7-day moving average
# --------------------------------------------------------------------------- #

def record_snapshots(rows: Iterable[dict[str, Any]], ts: str | None = None) -> int:
    """Insert bazaar rows, deduplicated on (item_id, ts)."""
    ts = ts or utcnow()
    payload = [
        (
            r["item_id"], ts, r["buy_price"], r["sell_price"], r["mid_price"],
            r.get("buy_volume", 0), r.get("sell_volume", 0),
        )
        for r in rows
    ]
    with connection() as conn:
        cur = conn.executemany(
            """INSERT OR IGNORE INTO market_snapshots
               (item_id, ts, buy_price, sell_price, mid_price, buy_volume, sell_volume)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            payload,
        )
        return cur.rowcount


def price_history(item_id: str, days: int = 7) -> list[tuple[str, float]]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connection() as conn:
        rows = conn.execute(
            "SELECT ts, mid_price FROM market_snapshots WHERE item_id = ? AND ts >= ? "
            "ORDER BY ts ASC",
            (item_id, since),
        ).fetchall()
    return [(r["ts"], r["mid_price"]) for r in rows]


def latest_prices() -> dict[str, dict[str, float]]:
    """Most recent snapshot per item (used by the portfolio report)."""
    with connection() as conn:
        rows = conn.execute(
            """SELECT s.item_id, s.buy_price, s.sell_price, s.mid_price, s.ts
               FROM market_snapshots s
               JOIN (SELECT item_id, MAX(ts) AS max_ts FROM market_snapshots GROUP BY item_id) m
                 ON m.item_id = s.item_id AND m.max_ts = s.ts"""
        ).fetchall()
    return {
        r["item_id"]: {
            "buy_price": r["buy_price"],
            "sell_price": r["sell_price"],
            "mid_price": r["mid_price"],
            "ts": r["ts"],
        }
        for r in rows
    }


# --------------------------------------------------------------------------- #
# Signals & feedback loop
# --------------------------------------------------------------------------- #

SIGNAL_FIELDS = (
    "item_id", "item_name", "signal", "confidence", "entry_price", "target_price",
    "stop_price", "risk_ratio", "ma7", "sector", "horizon_days", "catalysts",
    "thesis", "source", "commit_sha", "sources", "confluence_score", "risk_tier",
    "bull_case", "bear_case", "term", "expected_days_to_target",
    "expected_target_date", "expected_return_pct", "forecast_method", "catalyst_date",
)


def save_signal(signal: dict[str, Any]) -> int:
    data = {k: signal.get(k) for k in SIGNAL_FIELDS}
    data["term"] = (signal.get("term") or "SHORT").upper()
    data["created_at"] = signal.get("created_at") or utcnow()
    data["catalysts"] = json.dumps(signal.get("catalysts") or [])
    data["sources"] = json.dumps(signal.get("sources") or [])
    cols = ", ".join(data.keys())
    marks = ", ".join("?" for _ in data)
    with connection() as conn:
        cur = conn.execute(f"INSERT INTO signals ({cols}) VALUES ({marks})", tuple(data.values()))
        return int(cur.lastrowid)


def list_signals(
    status: str | None = None, limit: int = 200, term: str | None = None
) -> list[dict[str, Any]]:
    query = "SELECT * FROM signals"
    clauses: list[str] = []
    params: list[Any] = []
    if status and status != "all":
        clauses.append("status = ?")
        params.append(status)
    if term and term != "all":
        clauses.append("term = ?")
        params.append(term.upper())
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    with connection() as conn:
        rows = conn.execute(query, params).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        for key in ("catalysts", "sources"):
            try:
                item[key] = json.loads(item.get(key) or "[]")
            except json.JSONDecodeError:
                item[key] = []
        out.append(item)
    return out


def get_signal(signal_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    for key in ("catalysts", "sources"):
        try:
            item[key] = json.loads(item.get(key) or "[]")
        except json.JSONDecodeError:
            item[key] = []
    return item


def resolve_signal(
    signal_id: int, exit_price: float, reason: str | None = None
) -> dict[str, Any]:
    """Close a signal at exit_price and stamp won/lost based on the target."""
    signal = get_signal(signal_id)
    if not signal:
        raise ValueError(f"Signal {signal_id} not found")
    entry = float(signal.get("entry_price") or 0)
    if entry <= 0:
        raise ValueError(f"Signal {signal_id} has no valid entry price")

    pnl_pct = (float(exit_price) / entry - 1.0) * 100.0
    target = signal.get("target_price")
    stop = signal.get("stop_price")
    kind = signal["signal"].upper()
    if kind == "BUY":
        won = bool(target and exit_price >= float(target))
        if stop and exit_price <= float(stop):
            won = False
    elif kind == "HOLD":
        # Thesis: price stays inside the range. Won when it did.
        lo = float(stop) if stop else float("-inf")
        hi = float(target) if target else float("inf")
        won = lo <= float(exit_price) <= hi
    else:  # SELL / avoid calls win when price falls below entry
        won = pnl_pct < 0
    status = "won" if won else "lost"

    with connection() as conn:
        conn.execute(
            "UPDATE signals SET status = ?, exit_price = ?, pnl_pct = ?, "
            "resolved_at = ?, close_reason = ? WHERE id = ?",
            (
                status,
                float(exit_price),
                round(pnl_pct, 4),
                utcnow(),
                reason or "",
                signal_id,
            ),
        )
    return {"id": signal_id, "status": status, "pnl_pct": round(pnl_pct, 4)}


def expire_holds(hours: int = 24) -> int:
    """Retire stale HOLD calls so the book is not buried in watch notes."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat(timespec="seconds")
    with connection() as conn:
        cur = conn.execute(
            "UPDATE signals SET status = 'expired', resolved_at = ? "
            "WHERE status = 'open' AND signal = 'HOLD' AND created_at < ?",
            (utcnow(), cutoff),
        )
        return int(cur.rowcount)


def performance_stats(days: int = 30) -> dict[str, Any]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connection() as conn:
        closed = conn.execute(
            "SELECT status, pnl_pct FROM signals WHERE status IN ('won','lost') "
            "AND signal IN ('BUY','SELL') AND resolved_at >= ?",
            (since,),
        ).fetchall()
        open_count = conn.execute(
            "SELECT COUNT(*) AS n FROM signals WHERE status = 'open'"
        ).fetchone()["n"]
    wins = sum(1 for r in closed if r["status"] == "won")
    pnls = [r["pnl_pct"] for r in closed if r["pnl_pct"] is not None]
    win_rate = wins / len(closed) if closed else 0.5
    return {
        "window_days": days,
        "closed": len(closed),
        "wins": wins,
        "losses": len(closed) - wins,
        "win_rate": round(win_rate, 4),
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 3) if pnls else 0.0,
        "open": int(open_count),
    }


def recent_outcomes(limit: int = 12) -> list[dict[str, Any]]:
    """Wins/losses injected into the AI prompt for in-context learning."""
    with connection() as conn:
        rows = conn.execute(
            """SELECT item_id, signal, entry_price, exit_price, pnl_pct, status,
                      thesis, resolved_at
               FROM signals WHERE status IN ('won','lost')
               ORDER BY resolved_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# GitHub leak cache
# --------------------------------------------------------------------------- #

def saw_leak(sha: str) -> bool:
    with connection() as conn:
        row = conn.execute("SELECT 1 FROM leaks WHERE sha = ?", (sha,)).fetchone()
    return row is not None


def save_leak(leak: dict[str, Any]) -> None:
    with connection() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO leaks
               (sha, ts, title, author, files, diff, summary, items, seen, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (
                leak["sha"], leak.get("ts"), leak.get("title"), leak.get("author"),
                json.dumps(leak.get("files") or []),
                leak.get("diff"), leak.get("summary"),
                json.dumps(leak.get("items") or []),
                utcnow(),
            ),
        )


def recent_leaks(limit: int = 10, days: int | None = None) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM leaks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    leaks = []
    for r in rows:
        item = dict(r)
        for key in ("files", "items"):
            try:
                item[key] = json.loads(item.get(key) or "[]")
            except json.JSONDecodeError:
                item[key] = []
            leaks.append(item)
    return _fresh(leaks, "created_at", days)


# --------------------------------------------------------------------------- #
# Multi-source feeds (forum / YouTube / Reddit)
# --------------------------------------------------------------------------- #

def _saw(table: str, key_col: str, value: str) -> bool:
    with connection() as conn:
        row = conn.execute(f"SELECT 1 FROM {table} WHERE {key_col} = ?", (value,)).fetchone()
    return row is not None


def saw_forum_post(uid: str) -> bool:
    return _saw("forum_posts", "uid", uid)


def save_forum_posts(posts: Iterable[dict[str, Any]]) -> int:
    payload = [
        (
            p["uid"], p.get("title"), p.get("link"), p.get("published"),
            p.get("author"), (p.get("summary") or "")[:4000], p.get("feed"), utcnow(),
        )
        for p in posts
    ]
    with connection() as conn:
        cur = conn.executemany(
            """INSERT OR IGNORE INTO forum_posts
               (uid, title, link, published, author, summary, feed, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            payload,
        )
        return cur.rowcount


def recent_forum_posts(limit: int = 12, days: int | None = None) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM forum_posts ORDER BY published DESC LIMIT ?", (limit,)
        ).fetchall()
    return _fresh([dict(r) for r in rows], "published", days)


def saw_video(video_id: str) -> bool:
    return _saw("youtube_videos", "video_id", video_id)


def save_videos(videos: Iterable[dict[str, Any]]) -> int:
    payload = [
        (
            v["video_id"], v.get("title"), v.get("channel"), v.get("published"),
            v.get("link"), (v.get("transcript") or "")[:8000], utcnow(),
        )
        for v in videos
    ]
    with connection() as conn:
        cur = conn.executemany(
            """INSERT OR IGNORE INTO youtube_videos
               (video_id, title, channel, published, link, transcript, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            payload,
        )
        return cur.rowcount


def recent_videos(limit: int = 8, days: int | None = None) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM youtube_videos ORDER BY published DESC LIMIT ?", (limit,)
        ).fetchall()
    return _fresh([dict(r) for r in rows], "published", days)


def saw_reddit_post(post_id: str) -> bool:
    return _saw("reddit_posts", "post_id", post_id)


def save_reddit_posts(posts: Iterable[dict[str, Any]]) -> int:
    payload = [
        (
            p["post_id"], p.get("title"), p.get("author"), p.get("published"),
            p.get("link"), (p.get("body") or "")[:4000], int(p.get("score") or 0), utcnow(),
        )
        for p in posts
    ]
    with connection() as conn:
        cur = conn.executemany(
            """INSERT OR IGNORE INTO reddit_posts
               (post_id, title, author, published, link, body, score, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            payload,
        )
        return cur.rowcount


def recent_reddit_posts(limit: int = 15, days: int | None = None) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM reddit_posts ORDER BY published DESC LIMIT ?", (limit,),
        ).fetchall()
    return _fresh([dict(r) for r in rows], "published", days)


# --------------------------------------------------------------------------- #
# Auction House / mayor / wiki context
# --------------------------------------------------------------------------- #

def save_auction_stats(rows: Iterable[dict[str, Any]], ts: str | None = None) -> int:
    ts = ts or utcnow()
    payload = [
        (
            r["tag"].upper(), ts, r.get("min"), r.get("median"), r.get("mean"),
            r.get("mode"), r.get("max"), r.get("volume"), r.get("flags"), utcnow(),
        )
        for r in rows
    ]
    with connection() as conn:
        cur = conn.executemany(
            """INSERT OR IGNORE INTO auction_stats
               (tag, ts, min_price, median_price, mean_price, mode_price, max_price,
                volume, flags, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            payload,
        )
        return cur.rowcount


def recent_auction_stats(limit: int = 20, days: int | None = None) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT a.* FROM auction_stats a
               JOIN (SELECT tag, MAX(ts) AS max_ts FROM auction_stats GROUP BY tag) m
                 ON m.tag = a.tag AND m.max_ts = a.ts
               ORDER BY a.volume DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return _fresh([dict(r) for r in rows], "ts", days)


def lbin_history(tag: str, days: int = 7) -> list[dict[str, Any]]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connection() as conn:
        rows = conn.execute(
            "SELECT ts, median_price, volume FROM auction_stats "
            "WHERE tag = ? AND ts >= ? ORDER BY ts ASC",
            (tag.upper(), since),
        ).fetchall()
    return [dict(r) for r in rows]


def save_mayor_cycle(snapshot: dict[str, Any]) -> int:
    with connection() as conn:
        cur = conn.execute(
            """INSERT INTO mayor_cycles
               (ts, mayor_key, mayor_name, perks, election_year, candidates, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot.get("ts") or utcnow(),
                snapshot.get("mayor_key"),
                snapshot.get("mayor_name"),
                json.dumps(snapshot.get("perks") or []),
                snapshot.get("election_year"),
                json.dumps(snapshot.get("candidates") or []),
                utcnow(),
            ),
        )
        return int(cur.lastrowid)


def recent_mayor_cycles(limit: int = 5) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM mayor_cycles ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        for key in ("perks", "candidates"):
            try:
                item[key] = json.loads(item.get(key) or "[]")
            except json.JSONDecodeError:
                item[key] = []
        out.append(item)
    return out


def current_mayor() -> dict[str, Any] | None:
    cycles = recent_mayor_cycles(limit=1)
    return cycles[0] if cycles else None


def saw_wiki_rev(revid: str) -> bool:
    return _saw("wiki_changes", "revid", revid)


def save_wiki_changes(changes: Iterable[dict[str, Any]]) -> int:
    payload = [
        (
            str(c["revid"]), c.get("title"), c.get("user"), c.get("comment"),
            c.get("ts"), c.get("wiki"), utcnow(),
        )
        for c in changes
    ]
    with connection() as conn:
        cur = conn.executemany(
            """INSERT OR IGNORE INTO wiki_changes
               (revid, title, user, comment, ts, wiki, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            payload,
        )
        return cur.rowcount


def recent_wiki_changes(limit: int = 15, days: int | None = None) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM wiki_changes ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return _fresh([dict(r) for r in rows], "ts", days)


def save_web_results(results: Iterable[dict[str, Any]]) -> int:
    payload = [
        (
            r["result_id"], r.get("title"), r.get("url"), r.get("snippet"),
            r.get("query"), r.get("published"), utcnow(),
        )
        for r in results
    ]
    with connection() as conn:
        cur = conn.executemany(
            """INSERT OR IGNORE INTO web_results
               (result_id, title, url, snippet, query, published, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            payload,
        )
        return cur.rowcount


def recent_web_results(limit: int = 20, days: int | None = None) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM web_results ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return _fresh([dict(r) for r in rows], "published", days)


# --------------------------------------------------------------------------- #
# Portfolios
# --------------------------------------------------------------------------- #

def create_portfolio(name: str, purse: float) -> int:
    with connection() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO portfolios (name, purse, cash, created_at) VALUES (?, ?, ?, ?)",
            (name, float(purse), float(purse), utcnow()),
        )
        if cur.lastrowid:
            return int(cur.lastrowid)
        row = conn.execute("SELECT id FROM portfolios WHERE name = ?", (name,)).fetchone()
        return int(row["id"])


def list_portfolios() -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute("SELECT * FROM portfolios ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def update_purse(portfolio_id: int, purse: float) -> None:
    with connection() as conn:
        conn.execute("UPDATE portfolios SET purse = ? WHERE id = ?", (float(purse), portfolio_id))


def add_holding(portfolio_id: int, item_id: str, qty: float, cost_basis: float) -> int:
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO holdings (portfolio_id, item_id, qty, cost_basis, opened_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (portfolio_id, item_id.upper(), float(qty), float(cost_basis), utcnow()),
        )
        return int(cur.lastrowid)


def list_holdings(portfolio_id: int) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM holdings WHERE portfolio_id = ? ORDER BY id", (portfolio_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_holding(holding_id: int) -> None:
    """Drop a position; planned partial exits on it can no longer fire."""
    with connection() as conn:
        conn.execute(
            "DELETE FROM exit_orders WHERE holding_id = ? AND status = 'pending'",
            (holding_id,),
        )
        conn.execute("DELETE FROM holdings WHERE id = ?", (holding_id,))


# --------------------------------------------------------------------------- #
# Partial exit orders (sell N units at a chosen price)
# --------------------------------------------------------------------------- #

def add_exit_order(
    portfolio_id: int, holding_id: int, item_id: str, qty: float, target_price: float
) -> int:
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO exit_orders "
            "(portfolio_id, holding_id, item_id, qty, target_price, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (
                portfolio_id,
                holding_id,
                item_id.upper(),
                float(qty),
                float(target_price),
                utcnow(),
            ),
        )
        return int(cur.lastrowid)


def list_exit_orders(
    portfolio_id: int | None = None, status: str | None = "pending"
) -> list[dict[str, Any]]:
    query = "SELECT * FROM exit_orders"
    clauses: list[str] = []
    params: list[Any] = []
    if portfolio_id is not None:
        clauses.append("portfolio_id = ?")
        params.append(portfolio_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id"
    with connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def mark_exit_order_hit(order_id: int, price: float) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE exit_orders SET status = 'hit', hit_at = ?, hit_price = ? WHERE id = ?",
            (utcnow(), float(price), order_id),
        )


def delete_exit_order(order_id: int) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM exit_orders WHERE id = ?", (order_id,))


# --------------------------------------------------------------------------- #
# Realised cash-outs (book a sale: shrink the holding, credit the purse)
# --------------------------------------------------------------------------- #

def record_cashout(
    portfolio_id: int, holding_id: int, qty: float, price: float
) -> dict[str, Any]:
    """Sell `qty` units of a holding at `price`, in one atomic step.

    Reduces (or removes) the holding, credits proceeds to the purse and logs
    realised P&L. Pending partial exits are settled too: orders priced at or
    below the sale price fill, and anything left after a full exit is retired
    so the plan cannot outlive the position.

    Returns the logged cash-out row, or raises ValueError on bad input.
    """
    qty = float(qty)
    price = float(price)
    if qty <= 0 or price <= 0:
        raise ValueError("Quantity and price must both be greater than zero.")
    with connection() as conn:
        row = conn.execute("SELECT * FROM holdings WHERE id = ?", (holding_id,)).fetchone()
        if row is None:
            raise ValueError("Holding no longer exists.")
        if row["portfolio_id"] != portfolio_id:
            raise ValueError("Holding belongs to another portfolio.")
        held = float(row["qty"])
        if qty > held + 1e-9:
            raise ValueError(f"Only {held:,.0f} units held.")
        basis = float(row["cost_basis"])
        pnl = qty * (price - basis)
        pnl_pct = (price / basis - 1) * 100 if basis else 0.0
        sold_at = utcnow()
        remaining = held - qty
        if remaining <= 1e-9:
            conn.execute("DELETE FROM holdings WHERE id = ?", (holding_id,))
        else:
            conn.execute("UPDATE holdings SET qty = ? WHERE id = ?", (remaining, holding_id))
        conn.execute(
            "UPDATE portfolios SET purse = purse + ? WHERE id = ?",
            (qty * price, portfolio_id),
        )
        cur = conn.execute(
            "INSERT INTO cashouts "
            "(portfolio_id, holding_id, item_id, qty, price, cost_basis, pnl, pnl_pct, sold_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                portfolio_id,
                holding_id,
                row["item_id"],
                qty,
                price,
                basis,
                pnl,
                pnl_pct,
                sold_at,
            ),
        )
        # Settle planned exits against this sale.
        orders = conn.execute(
            "SELECT id FROM exit_orders WHERE holding_id = ? AND status = 'pending' "
            "AND target_price <= ? ORDER BY target_price, id",
            (holding_id, price),
        ).fetchall()
        for order in orders:
            conn.execute(
                "UPDATE exit_orders SET status = 'hit', hit_at = ?, hit_price = ? WHERE id = ?",
                (sold_at, price, order["id"]),
            )
        if remaining <= 1e-9:
            conn.execute(
                "UPDATE exit_orders SET status = 'cancelled' "
                "WHERE holding_id = ? AND status = 'pending'",
                (holding_id,),
            )
        return {
            "id": int(cur.lastrowid),
            "item_id": row["item_id"],
            "qty": qty,
            "price": price,
            "cost_basis": basis,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "sold_at": sold_at,
        }


def list_cashouts(portfolio_id: int, limit: int = 200) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM cashouts WHERE portfolio_id = ? ORDER BY id DESC LIMIT ?",
            (portfolio_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_cashout(cashout_id: int) -> None:
    """Remove a realised cash-out from the statement log.

    Record-keeping only: the holding reduction and purse credit already
    happened, so they are left untouched.
    """
    with connection() as conn:
        conn.execute("DELETE FROM cashouts WHERE id = ?", (cashout_id,))


# --------------------------------------------------------------------------- #
# Telemetry / settings
# --------------------------------------------------------------------------- #

def log_model_run(
    purpose: str, latency_ms: int, ok: bool, error: str = "", model: str | None = None
) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT INTO model_runs (ts, purpose, model, latency_ms, ok, error) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (utcnow(), purpose, model or config.CEREBRAS_MODEL, latency_ms, int(ok), error[:500]),
        )


def log_llm_usage(
    model: str,
    purpose: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    key_fp: str,
) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT INTO llm_usage (ts, model, purpose, prompt_tokens, completion_tokens, "
            "cost_usd, key_fp) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (utcnow(), model, purpose, int(prompt_tokens), int(completion_tokens), float(cost_usd), key_fp),
        )


def recent_llm_usage(
    limit: int = 12, key_fp: str | None = None
) -> list[dict[str, Any]]:
    query = "SELECT * FROM llm_usage"
    params: list[Any] = []
    if key_fp:
        query += " WHERE key_fp = ?"
        params.append(key_fp)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with connection() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def model_health(limit: int = 20) -> dict[str, Any]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT ok, latency_ms, ts FROM model_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    if not rows:
        return {"runs": 0, "success_rate": 0.0, "avg_latency_ms": 0}
    return {
        "runs": len(rows),
        "success_rate": round(sum(r["ok"] for r in rows) / len(rows), 3),
        "avg_latency_ms": int(sum(r["latency_ms"] or 0 for r in rows) / len(rows)),
    }


def get_setting(key: str, default: str = "") -> str:
    with connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# --------------------------------------------------------------------------- #
# Forecast grading: every prediction is scored at its own horizon, whether or
# not the user ever traded it. That record drives calibration.
# --------------------------------------------------------------------------- #

def price_at_or_before(item_id: str, ts: str) -> float | None:
    """Latest mid price at or before `ts` (used to grade a prediction)."""
    with connection() as conn:
        row = conn.execute(
            "SELECT mid_price FROM market_snapshots WHERE item_id = ? AND ts <= ? "
            "ORDER BY ts DESC LIMIT 1",
            (item_id, ts),
        ).fetchone()
    return float(row["mid_price"]) if row else None


def signals_due_for_grading(now: str | None = None) -> list[dict[str, Any]]:
    """Open BUY/SELL calls whose predicted target date (or horizon) has passed."""
    now = now or utcnow()
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE status = 'open' "
            "AND signal IN ('BUY','SELL') AND grade IS NULL",
        ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        for key in ("catalysts", "sources"):
            try:
                item[key] = json.loads(item.get(key) or "[]")
            except json.JSONDecodeError:
                item[key] = []
        due = item.get("expected_target_date") or ""
        horizon = int(item.get("horizon_days") or 14)
        if not due:
            created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            due = (created + timedelta(days=horizon)).isoformat(timespec="seconds")
        if due <= now:
            item["_due_at"] = due
            out.append(item)
    return out


def save_prediction_grade(grade: dict[str, Any]) -> int:
    cols = (
        "signal_id", "term", "item_id", "signal", "confidence", "created_at",
        "graded_at", "horizon_end", "predicted_days", "actual_days",
        "predicted_target", "predicted_return_pct", "actual_price",
        "actual_return_pct", "grade", "accuracy", "note",
    )
    data = [grade.get(c) for c in cols]
    with connection() as conn:
        cur = conn.execute(
            f"INSERT INTO prediction_grades ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            tuple(data),
        )
        if grade.get("signal_id"):
            conn.execute(
                "UPDATE signals SET grade = ?, graded_at = ? WHERE id = ?",
                (grade.get("grade"), grade.get("graded_at"), grade["signal_id"]),
            )
        return int(cur.lastrowid)


def list_prediction_grades(
    term: str | None = None, days: int = 365, limit: int = 500
) -> list[dict[str, Any]]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    query = "SELECT * FROM prediction_grades WHERE graded_at >= ?"
    params: list[Any] = [since]
    if term and term != "all":
        query += " AND term = ?"
        params.append(term.upper())
    query += " ORDER BY graded_at DESC LIMIT ?"
    params.append(limit)
    with connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def _bucket(confidence: float | None) -> str:
    value = float(confidence or 0)
    if value >= 0.70:
        return "high"
    if value >= 0.50:
        return "mid"
    return "low"


def calibration_stats(days: int = 365) -> dict[str, Any]:
    """How accurate the desk's own predictions have been.

    `hit_rate` credits a partly-correct call at half, so a model that predicts
    direction but misses the target does not read as a total failure.
    """
    rows = list_prediction_grades(days=days, limit=2000)
    if not rows:
        return {
            "n": 0, "hit_rate": 0.0, "avg_accuracy": 0.0,
            "accurate": 0, "partial": 0, "inaccurate": 0,
            "by_term": {}, "by_bucket": {}, "days_ratio": None,
        }

    def summarise(items: list[dict]) -> dict[str, Any]:
        n = len(items)
        acc = sum(1 for r in items if r["grade"] == "accurate")
        part = sum(1 for r in items if r["grade"] == "partially_accurate")
        inacc = sum(1 for r in items if r["grade"] == "inaccurate")
        accuracies = [float(r.get("accuracy") or 0) for r in items]
        ratios = [
            float(r["actual_days"]) / float(r["predicted_days"])
            for r in items
            if r.get("actual_days") and r.get("predicted_days")
            and r["grade"] in ("accurate", "partially_accurate")
            and float(r["predicted_days"]) > 0
        ]
        return {
            "n": n,
            "accurate": acc,
            "partial": part,
            "inaccurate": inacc,
            "hit_rate": round((acc + 0.5 * part) / n, 3) if n else 0.0,
            "avg_accuracy": round(sum(accuracies) / n, 3) if n else 0.0,
            "days_ratio": round(sum(ratios) / len(ratios), 3) if ratios else None,
        }

    by_term: dict[str, dict] = {}
    for term in ("SHORT", "LONG"):
        subset = [r for r in rows if (r.get("term") or "SHORT") == term]
        if subset:
            by_term[term] = summarise(subset)
    by_bucket: dict[str, dict] = {}
    for key in ("low", "mid", "high"):
        subset = [r for r in rows if _bucket(r.get("confidence")) == key]
        if subset:
            by_bucket[key] = summarise(subset)

    overall = summarise(rows)
    overall["by_term"] = by_term
    overall["by_bucket"] = by_bucket
    return overall


def record_calibration(calibration: dict[str, Any], time_scale: float | None = None) -> None:
    set_setting("calibration_snapshot", json.dumps(calibration, default=str))
    if time_scale:
        set_setting("forecast_time_scale", f"{time_scale:.4f}")


# --------------------------------------------------------------------------- #
# Update calendar (announced / predicted game updates mapped to sectors)
# --------------------------------------------------------------------------- #

def save_update_event(event: dict[str, Any]) -> None:
    with connection() as conn:
        conn.execute(
            """INSERT INTO update_calendar
               (key, title, expected_date, window_days, confidence, status, source,
                url, sectors, items, notes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 title = excluded.title,
                 expected_date = excluded.expected_date,
                 window_days = excluded.window_days,
                 confidence = excluded.confidence,
                 status = excluded.status,
                 source = excluded.source,
                 url = excluded.url,
                 sectors = excluded.sectors,
                 items = excluded.items,
                 notes = excluded.notes,
                 updated_at = excluded.updated_at""",
            (
                event["key"],
                event.get("title") or event["key"],
                event.get("expected_date") or "",
                int(event.get("window_days") or 30),
                float(event.get("confidence") or 0.5),
                event.get("status") or "predicted",
                event.get("source") or "",
                event.get("url") or "",
                json.dumps(event.get("sectors") or []),
                json.dumps(event.get("items") or []),
                (event.get("notes") or "")[:1200],
                utcnow(),
                utcnow(),
            ),
        )


def upcoming_updates(days: int = 120, limit: int = 30) -> list[dict[str, Any]]:
    today = datetime.now(timezone.utc).date().isoformat()
    horizon = (datetime.now(timezone.utc).date() + timedelta(days=days)).isoformat()
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM update_calendar WHERE "
            "(expected_date = '' OR (expected_date >= ? AND expected_date <= ?)) "
            "ORDER BY (expected_date = '') ASC, expected_date ASC, confidence DESC LIMIT ?",
            (today, horizon, limit),
        ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        for key in ("sectors", "items"):
            try:
                item[key] = json.loads(item.get(key) or "[]")
            except json.JSONDecodeError:
                item[key] = []
        out.append(item)
    return out


def list_update_calendar(limit: int = 60) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM update_calendar ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        for key in ("sectors", "items"):
            try:
                item[key] = json.loads(item.get(key) or "[]")
            except json.JSONDecodeError:
                item[key] = []
        out.append(item)
    return out


def save_research_plan(term: str, payload: dict[str, Any]) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT INTO research_plans (term, created_at, payload) VALUES (?, ?, ?)",
            (term.upper(), utcnow(), json.dumps(payload, default=str)),
        )
        conn.execute(
            "DELETE FROM research_plans WHERE term = ? AND id NOT IN "
            "(SELECT id FROM research_plans WHERE term = ? ORDER BY id DESC LIMIT 20)",
            (term.upper(), term.upper()),
        )


def latest_research_plan(term: str = "SHORT") -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT payload FROM research_plans WHERE term = ? ORDER BY id DESC LIMIT 1",
            (term.upper(),),
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
