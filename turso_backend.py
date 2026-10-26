"""Turso (libSQL) backend for the Streamlit Community Cloud deployment.

Community Cloud wipes its container filesystem on sleep/redeploy, so when
TURSO_DATABASE_URL is set every query goes to a remote libSQL database instead
of the local sqlite file. The desktop app keeps using sqlite untouched.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any, Iterable

_local = threading.local()


class _Cursor:
    """sqlite3.Cursor look-alike that always hands back dict rows."""

    def __init__(self, raw: Any = None, lastrowid: Any = None) -> None:
        self._raw = raw
        self._lastrowid = lastrowid

    @property
    def lastrowid(self) -> Any:
        if self._lastrowid is not None:
            return self._lastrowid
        return getattr(self._raw, "lastrowid", None)

    @property
    def rowcount(self) -> int:
        return getattr(self._raw, "rowcount", -1)

    @property
    def description(self) -> Any:
        return getattr(self._raw, "description", None)

    def _keys(self) -> list[str]:
        return [col[0] for col in (self.description or [])]

    def _row(self, raw: Any) -> Any:
        if raw is None or isinstance(raw, dict):
            return raw
        if isinstance(raw, sqlite3.Row):
            return dict(raw)
        return dict(zip(self._keys(), raw))

    def fetchone(self) -> Any:
        if self._raw is None:
            return None
        return self._row(self._raw.fetchone())

    def fetchall(self) -> list[Any]:
        if self._raw is None:
            return []
        return [self._row(row) for row in self._raw.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class TursoConnection:
    """Thin adapter that keeps database.py's sqlite3-style call sites working."""

    # Remote libSQL ignores these; the local file backend still honours them.
    _SKIP = ("PRAGMA JOURNAL_MODE", "PRAGMA FOREIGN_KEYS", "PRAGMA SYNCHRONOUS")

    def __init__(self, url: str, token: str) -> None:
        import libsql_experimental as libsql

        self._raw = libsql.connect(url, auth_token=token)

    def execute(self, sql: str, params: Iterable[Any] = ()) -> _Cursor:
        upper = sql.lstrip().upper()
        if upper.startswith(self._SKIP):
            return _Cursor()
        params = tuple(params)
        cur = self._raw.execute(sql, params) if params else self._raw.execute(sql)
        rowid = getattr(cur, "lastrowid", None)
        # Some libSQL builds only expose the id via a follow-up query.
        if rowid is None and upper.startswith("INSERT"):
            fallback = self._raw.execute("SELECT last_insert_rowid() AS id")
            found = fallback.fetchone()
            rowid = found[0] if found else None
        return _Cursor(cur, lastrowid=rowid)

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> _Cursor:
        return _Cursor(self._raw.executemany(sql, list(rows)))

    def executescript(self, script: str) -> _Cursor:
        for statement in script.split(";"):
            statement = statement.strip()
            if statement:
                self._raw.execute(statement)
        return _Cursor()

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        """Connections are cached per thread, so closing is a no-op."""


def connect(url: str, token: str) -> TursoConnection:
    """One connection per thread: avoids a handshake on every DB call."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = TursoConnection(url, token)
        _local.conn = conn
    return conn
