"""SQLite store for runs, events and the per-record cache."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
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
"""


class Store:
    def __init__(self, path: str | Path = ".agentl/agentl.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def start_run(self, run_id: str, pipeline: str, diagram: str) -> None:
        self._exec(
            "INSERT INTO runs (id, pipeline, status, started, diagram) VALUES (?, ?, 'running', ?, ?)",
            (run_id, pipeline, time.time(), diagram),
        )

    def finish_run(self, run_id: str, status: str, error: str | None = None) -> None:
        self._exec(
            "UPDATE runs SET status = ?, finished = ?, error = ? WHERE id = ?",
            (status, time.time(), error, run_id),
        )

    def event(self, run_id: str, type: str, step: str | None, data: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO events (run_id, ts, type, step, data) VALUES (?, ?, ?, ?, ?)",
            (run_id, time.time(), type, step, json.dumps(data, default=str)),
        )

    def cache_get(self, key: str) -> Any | None:
        rows = self._query("SELECT value FROM cache WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else None

    def cache_set(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT OR REPLACE INTO cache (key, value, ts) VALUES (?, ?, ?)",
            (key, json.dumps(value, default=str), time.time()),
        )

    def runs(self, limit: int = 20) -> list[dict]:
        rows = self._query(
            "SELECT id, pipeline, status, started, finished, error FROM runs ORDER BY started DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def run(self, run_id: str) -> dict | None:
        rows = self._query("SELECT * FROM runs WHERE id = ?", (run_id,))
        return dict(rows[0]) if rows else None

    def events(self, run_id: str) -> list[dict]:
        rows = self._query("SELECT ts, type, step, data FROM events WHERE run_id = ? ORDER BY id", (run_id,))
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]
