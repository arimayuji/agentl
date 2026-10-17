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
CREATE TABLE IF NOT EXISTS samples (
  run_id TEXT, step TEXT, records TEXT, total INTEGER, PRIMARY KEY (run_id, step)
);
CREATE TABLE IF NOT EXISTS rejected (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, pipeline TEXT, step TEXT, ts REAL,
  error TEXT, record TEXT, retried_by TEXT
);
CREATE INDEX IF NOT EXISTS rejected_pipeline ON rejected(pipeline, step);
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
    """CREATE TABLE IF NOT EXISTS agentl_samples (
      run_id TEXT, step TEXT, records JSONB, total INTEGER, PRIMARY KEY (run_id, step))""",
    """CREATE TABLE IF NOT EXISTS agentl_rejected (
      id BIGSERIAL PRIMARY KEY, run_id TEXT, pipeline TEXT, step TEXT, ts DOUBLE PRECISION,
      error TEXT, record JSONB, retried_by TEXT)""",
    "CREATE INDEX IF NOT EXISTS agentl_rejected_pipeline ON agentl_rejected(pipeline, step)",
]


class Store:
    """SQLite store. Thread-safe within one process."""

    T: ClassVar[dict[str, str]] = {
        "runs": "runs",
        "events": "events",
        "cache": "cache",
        "state": "state",
        "samples": "samples",
        "rejected": "rejected",
    }
    UPSERT_CACHE = "INSERT OR REPLACE INTO {cache} (key, value, ts) VALUES (?, ?, ?)"
    UPSERT_STATE = "INSERT OR REPLACE INTO {state} (pipeline, step, value, run_id, ts) VALUES (?, ?, ?, ?, ?)"
    INSERT_REJECTED = (
        "INSERT INTO {rejected} (run_id, pipeline, step, ts, error, record) VALUES (?, ?, ?, ?, ?, ?)"
    )
    UPSERT_SAMPLE = "INSERT OR REPLACE INTO {samples} (run_id, step, records, total) VALUES (?, ?, ?, ?)"

    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn: Any = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SQLITE_SCHEMA)
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(runs)")}
        if "outcome" not in cols:  # stores created before #188
            self._conn.execute("ALTER TABLE runs ADD COLUMN outcome TEXT")
            self._conn.commit()

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

    def finish_run(
        self, run_id: str, status: str, error: str | None = None, outcome: str | None = None
    ) -> None:
        self._exec(
            "UPDATE {runs} SET status = ?, finished = ?, error = ?, outcome = ? WHERE id = ?",
            (status, time.time(), error, outcome, run_id),
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

    # column mappings of map_schema steps (#177), per pipeline and step: {"layouts": {sig: mapping}}
    MAPPING_PREFIX: ClassVar[str] = "map_schema:"

    def mapping_get(self, pipeline: str, step: str) -> dict | None:
        return self.cache_get(f"{self.MAPPING_PREFIX}{pipeline}:{step}")

    def mapping_set(self, pipeline: str, step: str, value: dict) -> None:
        self.cache_set(f"{self.MAPPING_PREFIX}{pipeline}:{step}", value)

    def mapping_list(self, pipeline: str | None = None) -> list[dict]:
        """[{pipeline, step, layouts}] of every stored mapping, sorted."""
        rows = self._query(
            "SELECT key, value FROM {cache} WHERE key LIKE ? ORDER BY key", (self.MAPPING_PREFIX + "%",)
        )
        out = []
        for r in rows:
            pipe, _, step = r["key"][len(self.MAPPING_PREFIX) :].rpartition(":")
            if pipeline is None or pipe == pipeline:
                out.append({"pipeline": pipe, "step": step, **self._load(r["value"])})
        return out

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
            "SELECT id, pipeline, status, outcome, started, finished, error FROM {runs} "
            "ORDER BY started DESC LIMIT ?",
            (limit,),
        )

    def history(self, since: float, pipeline: str | None = None) -> list[dict]:
        """Every run started at or after `since` (epoch seconds), oldest first."""
        where = "started >= ?" + (" AND pipeline = ?" if pipeline else "")
        params: tuple = (since, pipeline) if pipeline else (since,)
        cols = "id, pipeline, status, outcome, started, finished, error"
        return self._query(f"SELECT {cols} FROM {{runs}} WHERE {where} ORDER BY started", params)

    def recent_runs(self, pipeline: str, limit: int) -> list[dict]:
        """The latest runs of one pipeline, newest first."""
        return self._query(
            "SELECT id, status, outcome, started FROM {runs} WHERE pipeline = ? "
            "ORDER BY started DESC LIMIT ?",
            (pipeline, limit),
        )

    def events_of(self, since: float, types: tuple[str, ...]) -> list[dict]:
        """Events of the given types recorded at or after `since`, for aggregate views."""
        marks = ", ".join("?" for _ in types)
        rows = self._query(
            "SELECT run_id, ts, type, step, data FROM {events} "
            f"WHERE ts >= ? AND type IN ({marks}) ORDER BY id",
            (since, *types),
        )
        return [{**r, "data": self._load(r["data"])} for r in rows]

    def sample_set(self, run_id: str, step: str, records: list[dict], total: int) -> None:
        """The first records a step produced in a run, for `show_output` (#164)."""
        self._exec(self.UPSERT_SAMPLE, (run_id, step, json.dumps(records, default=str), total))

    def sample(self, run_id: str, step: str) -> dict | None:
        rows = self._query(
            "SELECT records, total FROM {samples} WHERE run_id = ? AND step = ?", (run_id, step)
        )
        if not rows:
            return None
        return {"records": self._load(rows[0]["records"]), "total": rows[0]["total"]}

    # quarantine: rejected records, kept to list and retry (#180)
    def rejected_add(self, run_id: str, pipeline: str, step: str, records: list[dict]) -> None:
        now = time.time()
        for r in records:
            error = r.get("_error") if isinstance(r, dict) else None
            row = (run_id, pipeline, step, now, None if error is None else str(error))
            self._exec(self.INSERT_REJECTED, (*row, json.dumps(r, default=str)))

    def rejected_list(
        self,
        pipeline: str | None = None,
        step: str | None = None,
        pending: bool = False,
        limit: int | None = None,
    ) -> list[dict]:
        """Rejected records, oldest first. `pending`: only the ones no retry has taken yet."""
        where: list[str] = []
        params: list[Any] = []
        if pipeline:
            where.append("pipeline = ?")
            params.append(pipeline)
        if step:
            where.append("step = ?")
            params.append(step)
        if pending:
            where.append("retried_by IS NULL")
        sql = "SELECT id, run_id, pipeline, step, ts, error, record, retried_by FROM {rejected}"
        sql += (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._query(sql, tuple(params))
        return [{**r, "record": self._load(r["record"])} for r in rows]

    def rejected_get(self, rejected_id: int) -> dict | None:
        rows = self._query(
            "SELECT id, run_id, pipeline, step, ts, error, record, retried_by FROM {rejected} WHERE id = ?",
            (rejected_id,),
        )
        return {**rows[0], "record": self._load(rows[0]["record"])} if rows else None

    def rejected_retried(self, ids: list[int], run_id: str) -> None:
        for i in ids:
            self._exec("UPDATE {rejected} SET retried_by = ? WHERE id = ?", (run_id, i))

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
    Needs `agentl-etl[postgres]`. Tables: agentl_runs, agentl_events, agentl_cache, agentl_state,
    agentl_samples, agentl_rejected."""

    T: ClassVar[dict[str, str]] = {
        "runs": "agentl_runs",
        "events": "agentl_events",
        "cache": "agentl_cache",
        "state": "agentl_state",
        "samples": "agentl_samples",
        "rejected": "agentl_rejected",
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
    INSERT_REJECTED = (
        "INSERT INTO {rejected} (run_id, pipeline, step, ts, error, record) VALUES (?, ?, ?, ?, ?, ?::jsonb)"
    )
    UPSERT_SAMPLE = (
        "INSERT INTO {samples} (run_id, step, records, total) VALUES (?, ?, ?::jsonb, ?) "
        "ON CONFLICT (run_id, step) DO UPDATE SET records = EXCLUDED.records, total = EXCLUDED.total"
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
        self._conn.execute("ALTER TABLE agentl_runs ADD COLUMN IF NOT EXISTS outcome TEXT")

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
