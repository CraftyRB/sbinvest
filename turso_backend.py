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

    def _execute_raw(self, sql: str, params: Iterable[Any] = ()) -> Any:
        """libSQL builds expose either Connection.execute or only a cursor."""
        run = getattr(self._raw, "execute", None)
        if run is None:
            run = self._raw.cursor().execute
        params = tuple(params)
        return run(sql, params) if params else run(sql)

    def execute(self, sql: str, params: Iterable[Any] = ()) -> _Cursor:
        upper = sql.lstrip().upper()
        if upper.startswith(self._SKIP):
            return _Cursor()
        cur = self._execute_raw(sql, params)
        rowid = getattr(cur, "lastrowid", None)
        # Some libSQL builds only expose the id via a follow-up query.
        if rowid is None and upper.startswith("INSERT"):
            found = self._execute_raw("SELECT last_insert_rowid() AS id").fetchone()
            if isinstance(found, dict):
                rowid = next(iter(found.values()), None)
            elif found is not None:
                rowid = found[0]
        return _Cursor(cur, lastrowid=rowid)

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> _Cursor:
        run = getattr(self._raw, "executemany", None)
        if run is None:
            run = self._raw.cursor().executemany
        return _Cursor(run(sql, list(rows)))

    def executescript(self, script: str) -> _Cursor:
        for statement in script.split(";"):
            statement = statement.strip()
            if statement:
                self._execute_raw(statement)
        return _Cursor()

    def commit(self) -> None:
        run = getattr(self._raw, "commit", None)
        if run is not None:
            run()

    def rollback(self) -> None:
        run = getattr(self._raw, "rollback", None)
        if run is not None:
            run()

    def close(self) -> None:
        """Connections are cached per thread, so closing is a no-op."""


def connect(url: str, token: str) -> TursoConnection:
    """One connection per thread: avoids a handshake on every DB call."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = TursoConnection(url, token)
        _local.conn = conn
    return conn
