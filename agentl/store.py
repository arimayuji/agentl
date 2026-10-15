"""Run store: runs, events and the per-record agent cache.

SQLite by default (a local file). Postgres when several workers must share history and cache:
pass a `postgresql://` URL to `open_store`, `--db` or `AGENTL_DB`."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, ClassVar

from .registry import require

DEFAULT_DB = ".agentl/agentl.db"

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, pipeline TEXT, status TEXT, started REAL, finished REAL,
  error TEXT, diagram TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts REAL, type TEXT,
  step TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id);
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS state (
  pipeline TEXT, step TEXT, value TEXT, run_id TEXT, ts REAL, PRIMARY KEY (pipeline, step)
);
"""

# Prefixed names so the store can live in an application database without clashes.
POSTGRES_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS agentl_runs (
      id TEXT PRIMARY KEY, pipeline TEXT, status TEXT, started DOUBLE PRECISION,
      finished DOUBLE PRECISION, error TEXT, diagram TEXT)""",
    """CREATE TABLE IF NOT EXISTS agentl_events (
      id BIGSERIAL PRIMARY KEY, run_id TEXT, ts DOUBLE PRECISION, type TEXT, step TEXT, data JSONB)""",
    "CREATE INDEX IF NOT EXISTS agentl_events_run ON agentl_events(run_id)",
    "CREATE INDEX IF NOT EXISTS agentl_runs_started ON agentl_runs(started DESC)",
    "CREATE TABLE IF NOT EXISTS agentl_cache (key TEXT PRIMARY KEY, value JSONB, ts DOUBLE PRECISION)",
    """CREATE TABLE IF NOT EXISTS agentl_state (
      pipeline TEXT, step TEXT, value JSONB, run_id TEXT, ts DOUBLE PRECISION,
      PRIMARY KEY (pipeline, step))""",
]


class Store:
    """SQLite store. Thread-safe within one process."""

    T: ClassVar[dict[str, str]] = {"runs": "runs", "events": "events", "cache": "cache", "state": "state"}
    UPSERT_CACHE = "INSERT OR REPLACE INTO {cache} (key, value, ts) VALUES (?, ?, ?)"
    UPSERT_STATE = "INSERT OR REPLACE INTO {state} (pipeline, step, value, run_id, ts) VALUES (?, ?, ?, ?, ?)"

    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn: Any = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SQLITE_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _sql(self, sql: str) -> str:
        return sql.format(**self.T)

    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(self._sql(sql), params)
            self._conn.commit()

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(self._sql(sql), params).fetchall()]

    @staticmethod
    def _load(value: Any) -> Any:
        return json.loads(value) if isinstance(value, str) else value

    def start_run(self, run_id: str, pipeline: str, diagram: str) -> None:
        self._exec(
            "INSERT INTO {runs} (id, pipeline, status, started, diagram) VALUES (?, ?, 'running', ?, ?)",
            (run_id, pipeline, time.time(), diagram),
        )

    def finish_run(self, run_id: str, status: str, error: str | None = None) -> None:
        self._exec(
            "UPDATE {runs} SET status = ?, finished = ?, error = ? WHERE id = ?",
            (status, time.time(), error, run_id),
        )

    def event(self, run_id: str, type: str, step: str | None, data: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO {events} (run_id, ts, type, step, data) VALUES (?, ?, ?, ?, ?)",
            (run_id, time.time(), type, step, json.dumps(data, default=str)),
        )

    def cache_get(self, key: str) -> Any | None:
        rows = self._query("SELECT value FROM {cache} WHERE key = ?", (key,))
        return self._load(rows[0]["value"]) if rows else None

    def cache_set(self, key: str, value: Any) -> None:
        self._exec(self.UPSERT_CACHE, (key, json.dumps(value, default=str), time.time()))

    # cursors of incremental steps, per pipeline and step
    def state_get(self, pipeline: str, step: str) -> Any | None:
        rows = self._query("SELECT value FROM {state} WHERE pipeline = ? AND step = ?", (pipeline, step))
        return self._load(rows[0]["value"])["v"] if rows else None

    def state_set(self, pipeline: str, step: str, value: Any, run_id: str) -> None:
        self._exec(
            self.UPSERT_STATE, (pipeline, step, json.dumps({"v": value}, default=str), run_id, time.time())
        )

    def state_list(self, pipeline: str | None = None) -> list[dict]:
        where, params = (" WHERE pipeline = ?", (pipeline,)) if pipeline else ("", ())
        rows = self._query(
            f"SELECT pipeline, step, value, run_id, ts FROM {{state}}{where} ORDER BY pipeline, step", params
        )
        return [{**r, "value": self._load(r["value"])["v"]} for r in rows]

    def state_reset(self, pipeline: str, step: str | None = None) -> int:
        n = len([r for r in self.state_list(pipeline) if step is None or r["step"] == step])
        if step is None:
            self._exec("DELETE FROM {state} WHERE pipeline = ?", (pipeline,))
        else:
            self._exec("DELETE FROM {state} WHERE pipeline = ? AND step = ?", (pipeline, step))
        return n

    def runs(self, limit: int = 20) -> list[dict]:
        return self._query(
            "SELECT id, pipeline, status, started, finished, error FROM {runs} ORDER BY started DESC LIMIT ?",
            (limit,),
        )

    def history(self, since: float, pipeline: str | None = None) -> list[dict]:
        """Every run started at or after `since` (epoch seconds), oldest first."""
        where = "started >= ?" + (" AND pipeline = ?" if pipeline else "")
        params: tuple = (since, pipeline) if pipeline else (since,)
        cols = "id, pipeline, status, started, finished, error"
        return self._query(f"SELECT {cols} FROM {{runs}} WHERE {where} ORDER BY started", params)

    def events_of(self, since: float, types: tuple[str, ...]) -> list[dict]:
        """Events of the given types recorded at or after `since`, for aggregate views."""
        marks = ", ".join("?" for _ in types)
        rows = self._query(
            "SELECT run_id, ts, type, step, data FROM {events} "
            f"WHERE ts >= ? AND type IN ({marks}) ORDER BY id",
            (since, *types),
        )
        return [{**r, "data": self._load(r["data"])} for r in rows]

    def run(self, run_id: str) -> dict | None:
        rows = self._query("SELECT * FROM {runs} WHERE id = ?", (run_id,))
        return rows[0] if rows else None

    def events(self, run_id: str) -> list[dict]:
        rows = self._query(
            "SELECT ts, type, step, data FROM {events} WHERE run_id = ? ORDER BY id", (run_id,)
        )
        return [{**r, "data": self._load(r["data"])} for r in rows]


class PostgresStore(Store):
    """Postgres store, shared by every worker that points to the same database.
    Needs `agentl-etl[postgres]`. Tables: agentl_runs, agentl_events, agentl_cache, agentl_state."""

    T: ClassVar[dict[str, str]] = {
        "runs": "agentl_runs",
        "events": "agentl_events",
        "cache": "agentl_cache",
        "state": "agentl_state",
    }
    UPSERT_CACHE = (
        "INSERT INTO {cache} (key, value, ts) VALUES (?, ?::jsonb, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, ts = EXCLUDED.ts"
    )
    UPSERT_STATE = (
        "INSERT INTO {state} (pipeline, step, value, run_id, ts) VALUES (?, ?, ?::jsonb, ?, ?) "
        "ON CONFLICT (pipeline, step) DO UPDATE SET value = EXCLUDED.value, run_id = EXCLUDED.run_id, "
        "ts = EXCLUDED.ts"
    )

    def __init__(self, dsn: str):
        psycopg = require("psycopg", "postgres")
        rows = require("psycopg.rows", "postgres")
        self.path = Path(".")
        self.dsn = dsn
        self._lock = threading.Lock()
        self._conn = psycopg.connect(dsn, autocommit=True, row_factory=rows.dict_row)
        for stmt in POSTGRES_SCHEMA:
            self._conn.execute(stmt)

    def _sql(self, sql: str) -> str:
        return super()._sql(sql).replace("?", "%s")

    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(self._sql(sql), params)

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return list(self._conn.execute(self._sql(sql), params).fetchall())

    def event(self, run_id: str, type: str, step: str | None, data: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO {events} (run_id, ts, type, step, data) VALUES (?, ?, ?, ?, ?::jsonb)",
            (run_id, time.time(), type, step, json.dumps(data, default=str)),
        )


def open_store(target: str | os.PathLike | None = None) -> Store:
    """`postgresql://...` -> PostgresStore; anything else is a SQLite file path.
    Defaults to `AGENTL_DB`, then `.agentl/agentl.db`."""
    target = str(target or os.environ.get("AGENTL_DB") or DEFAULT_DB)
    if target.startswith(("postgresql://", "postgres://")):
        return PostgresStore(target)
    return Store(target)
