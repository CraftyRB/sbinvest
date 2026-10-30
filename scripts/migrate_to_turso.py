"""Copy the local sqlite database into a remote Turso/libSQL database.

Streamlit Community Cloud has no durable disk, so the deployed app reads and
writes Turso. This one-off script lifts every local row across so the cloud
desk starts with the same signals, holdings and price history.

Usage:
    TURSO_AUTH_TOKEN=... python scripts/migrate_to_turso.py \
        --url libsql://db.turso.io [--only holdings,portfolios] [--verify]

The libSQL native wheel does not exist for Python 3.15, so rows go over the
HTTP pipeline endpoint instead.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterator

import requests

BATCH_STATEMENTS = 300  # rows folded into one multi-row INSERT
STATEMENTS_PER_REQUEST = 4
PIPELINE_TIMEOUT = 120

# Parents must land before their children: holdings references portfolios.
TABLE_ORDER = [
    "portfolios",
    "holdings",
    "exit_orders",
    "cashouts",
    "update_calendar",
    "signals",
    "prediction_grades",
]


def http_endpoint(url: str) -> str:
    base = url.replace("libsql://", "https://").replace("wss://", "https://").rstrip("/")
    return f"{base}/v2/pipeline"


def encode_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": "1" if value else "0"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, bytes):
        return {"type": "blob", "value": base64.b64encode(value).decode("ascii")}
    return {"type": "text", "value": str(value)}


class Turso:
    def __init__(self, url: str, token: str, attempts: int = 5) -> None:
        self.endpoint = http_endpoint(url)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                # Turso closes idle HTTP/1.1 sockets; asking for a close each time
                # avoids reusing a socket the server has already dropped.
                "Connection": "close",
            }
        )
        self.attempts = attempts

    def pipeline(self, statements: list[tuple[str, list[dict[str, Any]]]]) -> list[dict]:
        """Each statement is (sql, flat args); every row is its own execute."""
        payload = {
            "requests": [
                {"type": "execute", "stmt": {"sql": sql, "args": args}}
                for sql, args in statements
            ]
            + [{"type": "close"}]
        }
        for attempt in range(1, self.attempts + 1):
            try:
                resp = self.session.post(
                    self.endpoint, json=payload, timeout=PIPELINE_TIMEOUT
                )
            except requests.RequestException as exc:
                if attempt == self.attempts:
                    raise
                print(f"\n  {type(exc).__name__}, retrying ({attempt}/{self.attempts})...")
                time.sleep(2 * attempt)
                continue
            if resp.status_code >= 500 and attempt < self.attempts:
                print(f"\n  HTTP {resp.status_code}, retrying ({attempt}/{self.attempts})...")
                time.sleep(2 * attempt)
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
            results = resp.json().get("results", [])
            for result in results:
                if result.get("type") == "error":
                    raise RuntimeError(f"Turso rejected a statement: {result.get('error')}")
            return results
        raise RuntimeError("unreachable")

    def scalar(self, sql: str) -> Any:
        results = self.pipeline([(sql, [])])
        rows = results[0].get("response", {}).get("result", {}).get("rows", [])
        if not rows or not rows[0]:
            return None
        return rows[0][0].get("value")


def local_tables(conn: sqlite3.Connection) -> list[str]:
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    ordered = [t for t in TABLE_ORDER if t in names]
    return ordered + [t for t in names if t not in ordered]


def chunks(iterator: Iterator[tuple], size: int) -> Iterator[list[tuple]]:
    batch: list[tuple] = []
    for row in iterator:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def migrate(local: sqlite3.Connection, remote: Turso, only: set[str] | None) -> None:
    tables = [t for t in local_tables(local) if not only or t in only]
    for table in tables:
        cols = [row[1] for row in local.execute(f"PRAGMA table_info({table})")]
        total = local.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if not total:
            print(f"{table:22s} 0 rows, skipped")
            continue
        row_sql = f"({', '.join('?' for _ in cols)})"
        cursor = local.execute(f"SELECT {', '.join(cols)} FROM {table}")
        done = 0
        started = time.monotonic()
        statements: list[tuple[str, list[dict[str, Any]]]] = []
        for batch in chunks(iter(cursor), BATCH_STATEMENTS):
            values = ",".join(row_sql for _ in batch)
            sql = (
                f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES {values}"
            )
            args = [encode_value(v) for row in batch for v in row]
            statements.append((sql, args))
            done += len(batch)
            if len(statements) >= STATEMENTS_PER_REQUEST:
                remote.pipeline(statements)
                statements = []
                elapsed = max(time.monotonic() - started, 0.001)
                print(
                    f"\r{table:22s} {done:,}/{total:,} rows "
                    f"({done / elapsed:,.0f} rows/s)",
                    end="",
                    flush=True,
                )
        if statements:
            remote.pipeline(statements)
            elapsed = max(time.monotonic() - started, 0.001)
            print(
                f"\r{table:22s} {done:,}/{total:,} rows "
                f"({done / elapsed:,.0f} rows/s)",
                end="",
                flush=True,
            )
        print()


def verify(local: sqlite3.Connection, remote: Turso, only: set[str] | None) -> int:
    mismatches = 0
    for table in [t for t in local_tables(local) if not only or t in only]:
        expected = local.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        actual = remote.scalar(f"SELECT COUNT(*) FROM {table}") or 0
        flag = "ok" if int(actual) == expected else "MISMATCH"
        if int(actual) != expected:
            mismatches += 1
        print(f"{table:22s} local {expected:>8,}  remote {int(actual):>8,}  {flag}")
    return mismatches


def main() -> int:
    parser = argparse.ArgumentParser(description="Lift local sqlite data into Turso.")
    parser.add_argument("--url", default=os.getenv("TURSO_DATABASE_URL", ""))
    parser.add_argument("--token", default=os.getenv("TURSO_AUTH_TOKEN", ""))
    parser.add_argument("--db", default=str(Path(__file__).resolve().parent.parent / "investments.db"))
    parser.add_argument("--only", default="", help="comma separated table names")
    parser.add_argument("--verify", action="store_true", help="compare row counts only")
    args = parser.parse_args()

    if not args.url or not args.token:
        parser.error("need --url and --token (or TURSO_DATABASE_URL / TURSO_AUTH_TOKEN)")
    if not Path(args.db).exists():
        parser.error(f"local database not found: {args.db}")

    only = {t.strip() for t in args.only.split(",") if t.strip()} or None
    local = sqlite3.connect(args.db)
    remote = Turso(args.url, args.token)

    if args.verify:
        return 1 if verify(local, remote, only) else 0

    migrate(local, remote, only)
    print("\nRow counts after migration:")
    return 1 if verify(local, remote, only) else 0


if __name__ == "__main__":
    sys.exit(main())
