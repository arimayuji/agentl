"""Run store: runs, events and the per-record agent cache.

SQLite by default (a local file). Postgres when several workers must share history and cache:
pass a `postgresql://` URL to `open_store`, `--db` or `AGENTL_DB`."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
CREATE TABLE IF NOT EXISTS locks (key TEXT PRIMARY KEY, owner TEXT, expires REAL);
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
CREATE TABLE IF NOT EXISTS seen (
  scope TEXT, key TEXT, run_id TEXT, ts REAL, PRIMARY KEY (scope, key)
);
CREATE TABLE IF NOT EXISTS review (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, pipeline TEXT, step TEXT, key TEXT, ts REAL,
  reason TEXT, input TEXT, answer TEXT, status TEXT, value TEXT, reviewed REAL
);
CREATE INDEX IF NOT EXISTS review_key ON review(pipeline, step, key);
CREATE TABLE IF NOT EXISTS errors (
  pipeline TEXT, fingerprint TEXT, step TEXT, error_class TEXT, error TEXT, first_seen REAL,
  last_seen REAL, count INTEGER, status TEXT, alerted REAL, resolved REAL,
  PRIMARY KEY (pipeline, fingerprint)
);
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, pipeline TEXT, step TEXT, url TEXT,
  kind TEXT, sha256 TEXT, bytes INTEGER, content_type TEXT, captured_at REAL, location TEXT
);
CREATE INDEX IF NOT EXISTS evidence_run ON evidence(run_id);
CREATE INDEX IF NOT EXISTS evidence_source ON evidence(pipeline, step, url, kind);
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
    "CREATE TABLE IF NOT EXISTS agentl_locks (key TEXT PRIMARY KEY, owner TEXT, expires DOUBLE PRECISION)",
    """CREATE TABLE IF NOT EXISTS agentl_state (
      pipeline TEXT, step TEXT, value JSONB, run_id TEXT, ts DOUBLE PRECISION,
      PRIMARY KEY (pipeline, step))""",
    """CREATE TABLE IF NOT EXISTS agentl_samples (
      run_id TEXT, step TEXT, records JSONB, total INTEGER, PRIMARY KEY (run_id, step))""",
    """CREATE TABLE IF NOT EXISTS agentl_rejected (
      id BIGSERIAL PRIMARY KEY, run_id TEXT, pipeline TEXT, step TEXT, ts DOUBLE PRECISION,
      error TEXT, record JSONB, retried_by TEXT)""",
    "CREATE INDEX IF NOT EXISTS agentl_rejected_pipeline ON agentl_rejected(pipeline, step)",
    """CREATE TABLE IF NOT EXISTS agentl_seen (
      scope TEXT, key TEXT, run_id TEXT, ts DOUBLE PRECISION, PRIMARY KEY (scope, key))""",
    """CREATE TABLE IF NOT EXISTS agentl_review (
      id BIGSERIAL PRIMARY KEY, run_id TEXT, pipeline TEXT, step TEXT, key TEXT, ts DOUBLE PRECISION,
      reason TEXT, input TEXT, answer TEXT, status TEXT, value TEXT, reviewed DOUBLE PRECISION)""",
    "CREATE INDEX IF NOT EXISTS agentl_review_key ON agentl_review(pipeline, step, key)",
    """CREATE TABLE IF NOT EXISTS agentl_errors (
      pipeline TEXT, fingerprint TEXT, step TEXT, error_class TEXT, error TEXT,
      first_seen DOUBLE PRECISION, last_seen DOUBLE PRECISION, count INTEGER, status TEXT,
      alerted DOUBLE PRECISION, resolved DOUBLE PRECISION, PRIMARY KEY (pipeline, fingerprint))""",
    """CREATE TABLE IF NOT EXISTS agentl_evidence (
      id BIGSERIAL PRIMARY KEY, run_id TEXT, pipeline TEXT, step TEXT, url TEXT, kind TEXT,
      sha256 TEXT, bytes BIGINT, content_type TEXT, captured_at DOUBLE PRECISION, location TEXT)""",
    "CREATE INDEX IF NOT EXISTS agentl_evidence_run ON agentl_evidence(run_id)",
    "CREATE INDEX IF NOT EXISTS agentl_evidence_source ON agentl_evidence(pipeline, step, url, kind)",
]


# Ordered schema migrations: (version, table key in Store.T, column, SQLite type, Postgres type).
# The base schema above only ever gains new tables (CREATE ... IF NOT EXISTS); a change to an
# existing table is appended here and never edited. A new table is recorded with column None:
# the base schema creates it on open, so the step only moves the version. Each step is
# idempotent, so a store created before versioning (schema_version missing) replays them all safely.
MIGRATIONS: list[tuple[int, str, str | None, str, str]] = [
    (1, "runs", "outcome", "TEXT", "TEXT"),  # run outcomes (#188)
    (2, "runs", "labels", "TEXT", "TEXT"),  # run labels (#295)
    (3, "cache", "expires", "REAL", "DOUBLE PRECISION"),  # cache TTLs (#204)
    (4, "errors", None, "", ""),  # run errors by fingerprint (#356)
    (5, "evidence", None, "", ""),  # evidence artifact index (#293)
]
SCHEMA_VERSION = MIGRATIONS[-1][0]


class Store:
    """SQLite store. Thread-safe within one process."""

    T: ClassVar[dict[str, str]] = {
        "runs": "runs",
        "events": "events",
        "cache": "cache",
        "state": "state",
        "samples": "samples",
        "rejected": "rejected",
        "locks": "locks",
        "seen": "seen",
        "review": "review",
        "errors": "errors",
        "evidence": "evidence",
        "schema_version": "schema_version",
    }
    INSERT_SEEN = "INSERT OR IGNORE INTO {seen} (scope, key, run_id, ts) VALUES (?, ?, ?, ?)"
    UPSERT_CACHE = "INSERT OR REPLACE INTO {cache} (key, value, ts, expires) VALUES (?, ?, ?, ?)"
    # Take the lease when it is free or expired; a single statement, so atomic across processes.
    ACQUIRE_LOCK = (
        "INSERT INTO {locks} (key, owner, expires) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE "
        "SET owner = excluded.owner, expires = excluded.expires "
        "WHERE {locks}.expires < ? OR {locks}.owner = excluded.owner"
    )
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
        self._conn.execute("BEGIN IMMEDIATE")  # one process migrates at a time
        try:
            self._migrate()
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def _migrate(self) -> None:
        """Bring the schema to SCHEMA_VERSION. Callers hold the database write lock."""
        table = self.T["schema_version"]
        self._conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" (version INTEGER NOT NULL)')
        row = self._conn.execute(f'SELECT version FROM "{table}"').fetchone()
        current = row["version"] if row else 0
        for version, key, column, sqlite_type, pg_type in MIGRATIONS:
            if version > current and column is not None:
                self._add_column(self.T[key], column, sqlite_type, pg_type)
        if row is None:
            self._conn.execute(f'INSERT INTO "{table}" (version) VALUES ({SCHEMA_VERSION})')
        elif current < SCHEMA_VERSION:  # a newer store (later agentl) is left alone
            self._conn.execute(f'UPDATE "{table}" SET version = {SCHEMA_VERSION}')

    def _add_column(self, table: str, column: str, sqlite_type: str, pg_type: str) -> None:
        cols = {r[1] for r in self._conn.execute(f'PRAGMA table_info("{table}")')}
        if column not in cols:  # stores created before versioning may already have it
            self._conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {sqlite_type}')

    def schema_version(self) -> int:
        """The store's schema version (SCHEMA_VERSION once opened)."""
        rows = self._query("SELECT version FROM {schema_version}")
        return int(rows[0]["version"]) if rows else 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _sql(self, sql: str) -> str:
        return sql.format(**self.T)

    def _exec(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            n = self._conn.execute(self._sql(sql), params).rowcount
            self._conn.commit()
            return n

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(self._sql(sql), params).fetchall()]

    @staticmethod
    def _load(value: Any) -> Any:
        return json.loads(value) if isinstance(value, str) else value

    def start_run(
        self, run_id: str, pipeline: str, diagram: str, labels: dict[str, str] | None = None
    ) -> None:
        self._exec(
            "INSERT INTO {runs} (id, pipeline, status, started, diagram, labels) "
            "VALUES (?, ?, 'running', ?, ?, ?)",
            (run_id, pipeline, time.time(), diagram, json.dumps(labels or {}, sort_keys=True)),
        )

    def _with_labels(self, rows: list[dict], filters: dict[str, str] | None = None) -> list[dict]:
        from .labels import matches

        out = []
        for r in rows:
            r = {**r, "labels": self._load(r.get("labels")) or {}}
            if matches(r["labels"], filters):
                out.append(r)
        return out

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
        """The cached value, or None when missing or past its TTL."""
        rows = self._query(
            "SELECT value FROM {cache} WHERE key = ? AND (expires IS NULL OR expires > ?)", (key, time.time())
        )
        return self._load(rows[0]["value"]) if rows else None

    def cache_set(self, key: str, value: Any, ttl: float | None = None) -> None:
        """Store `value`; with `ttl` (seconds) it expires after that long."""
        now = time.time()
        expires = None if ttl is None else now + ttl
        self._exec(self.UPSERT_CACHE, (key, json.dumps(value, default=str), now, expires))

    def cache_clear(self, key: str | None = None) -> int:
        """Delete one key, every key starting with a prefix (`key*`), or all (None). Returns the count."""
        if key is None:
            return self._exec("DELETE FROM {cache}")
        if key.endswith("*"):
            prefix = key[:-1].replace("!", "!!").replace("%", "!%").replace("_", "!_")
            return self._exec("DELETE FROM {cache} WHERE key LIKE ? ESCAPE '!'", (prefix + "%",))
        return self._exec("DELETE FROM {cache} WHERE key = ?", (key,))

    def cache_compute(
        self,
        key: str,
        compute: Callable[[], Any],
        ttl: float | None = None,
        single_flight: bool = True,
        lease: float = 60.0,
        wait: float = 3600.0,
    ) -> Any:
        """The cached value of `key`, computing and storing it on a miss. With `single_flight`,
        concurrent callers (threads, processes or hosts sharing the store) compute it once: one
        holds a lease on the key, the others wait for its result. A lease of a dead process expires
        after `lease` seconds and another caller takes over. `None` results are not cached."""
        hit = self.cache_get(key)
        if hit is not None or not single_flight:
            if hit is None:
                hit = compute()
                if hit is not None:
                    self.cache_set(key, hit, ttl)
            return hit
        deadline = time.monotonic() + wait
        pause = 0.02
        while True:
            with self.lease("cache:" + key, lease) as held:
                if held:
                    hit = self.cache_get(key)  # filled while we were waiting for the lease
                    if hit is None:
                        hit = compute()
                        if hit is not None:
                            self.cache_set(key, hit, ttl)
                    return hit
            hit = self.cache_get(key)
            if hit is not None:
                return hit
            if time.monotonic() > deadline:
                raise TimeoutError(f"cache key {key!r}: still computed elsewhere after {wait:.0f}s")
            time.sleep(pause)
            pause = min(pause * 2, 1.0)

    # leases: locks that expire if their holder dies (#204)
    def lock_acquire(self, key: str, owner: str, ttl: float) -> bool:
        """Take the lease on `key` for `ttl` seconds. True when `owner` now holds it (also when it
        already did: that extends it)."""
        now = time.time()
        return self._exec(self.ACQUIRE_LOCK, (key, owner, now + ttl, now)) > 0

    def lock_refresh(self, key: str, owner: str, ttl: float) -> bool:
        return (
            self._exec(
                "UPDATE {locks} SET expires = ? WHERE key = ? AND owner = ?", (time.time() + ttl, key, owner)
            )
            > 0
        )

    def lock_release(self, key: str, owner: str) -> None:
        self._exec("DELETE FROM {locks} WHERE key = ? AND owner = ?", (key, owner))

    def lock_holder(self, key: str) -> dict | None:
        rows = self._query(
            "SELECT owner, expires FROM {locks} WHERE key = ? AND expires >= ?", (key, time.time())
        )
        return rows[0] if rows else None

    @contextmanager
    def lease(self, key: str, ttl: float, owner: str | None = None) -> Iterator[bool]:
        """Try once to take the lease on `key`; yields whether it was taken. While held, a thread
        renews it every `ttl / 3` seconds, so it lapses only when the process dies or hangs."""
        owner = owner or uuid.uuid4().hex
        if not self.lock_acquire(key, owner, ttl):
            yield False
            return
        stop = threading.Event()

        def renew() -> None:
            while not stop.wait(ttl / 3):
                try:
                    self.lock_refresh(key, owner, ttl)
                except Exception:  # the store is closing; the lease will lapse on its own
                    return

        beat = threading.Thread(target=renew, name=f"agentl-lease-{key[:20]}", daemon=True)
        beat.start()
        try:
            yield True
        finally:
            stop.set()
            beat.join()
            self.lock_release(key, owner)

    # request budgets shared by steps and runs (#212): a fixed window counted in the cache table
    def budget_take(self, key: str, limit: int, window: float) -> float:
        """Take one request from budget `key` (`limit` per `window` seconds, windows aligned to
        the clock). Returns 0 when taken, or the seconds until the window resets when it is spent.
        Atomic across threads, processes and hosts that share the store."""
        owner = uuid.uuid4().hex
        pause = 0.005
        while not self.lock_acquire("budget:" + key, owner, 10.0):
            time.sleep(pause)
            pause = min(pause * 2, 0.2)
        try:
            now = time.time()
            start = now - now % window
            slot = f"budget:{key}:{int(start)}"
            used = int(self.cache_get(slot) or 0)
            if used >= limit:
                return start + window - now
            self.cache_set(slot, used + 1, ttl=window * 2)
            return 0.0
        finally:
            self.lock_release("budget:" + key, owner)

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

    # keys `skip_seen` already let through, per scope
    def seen_filter(self, scope: str, keys: list[str]) -> set[str]:
        """The subset of `keys` already marked seen in `scope`."""
        found: set[str] = set()
        for i in range(0, len(keys), 500):
            chunk = keys[i : i + 500]
            marks = ", ".join("?" * len(chunk))
            rows = self._query(
                f"SELECT key FROM {{seen}} WHERE scope = ? AND key IN ({marks})", (scope, *chunk)
            )
            found.update(r["key"] for r in rows)
        return found

    def seen_add(self, scope: str, keys: list[str], run_id: str) -> None:
        now = time.time()
        for key in keys:
            self._exec(self.INSERT_SEEN, (scope, key, run_id, now))

    def seen_claim(self, scope: str, key: str, run_id: str) -> bool:
        """Mark one key; True only for the caller that inserted it (one statement, so atomic
        across threads, processes and hosts sharing the store)."""
        return self._exec(self.INSERT_SEEN, (scope, key, run_id, time.time())) > 0

    def seen_scopes(self, prefix: str = "") -> list[dict]:
        """Scopes starting with `prefix`: `{scope, keys, first, last}` (first and last key times)."""
        like = prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        return self._query(
            "SELECT scope, COUNT(*) AS keys, MIN(ts) AS first, MAX(ts) AS last FROM {seen} "
            "WHERE scope LIKE ? ESCAPE '!' GROUP BY scope ORDER BY scope",
            (like,),
        )

    def seen_list(self, scope: str | None = None) -> list[dict]:
        where, params = (" WHERE scope = ?", (scope,)) if scope else ("", ())
        return self._query(
            f"SELECT scope, key, run_id, ts FROM {{seen}}{where} ORDER BY scope, ts, key", params
        )

    def seen_forget(self, scope: str | None = None, key: str | None = None) -> int:
        """Forget one key, a whole scope, or everything; returns how many keys were removed."""
        rows = [r for r in self.seen_list(scope) if key is None or r["key"] == key]
        clauses = [c for c, v in (("scope = ?", scope), ("key = ?", key)) if v is not None]
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        self._exec(f"DELETE FROM {{seen}}{where}", tuple(v for v in (scope, key) if v is not None))
        return len(rows)

    def runs(self, limit: int = 20, labels: dict[str, str] | None = None) -> list[dict]:
        """The latest runs, newest first; with `labels`, only runs carrying all of them."""
        cols = "id, pipeline, status, outcome, started, finished, error, labels"
        if not labels:
            rows = self._query(f"SELECT {cols} FROM {{runs}} ORDER BY started DESC LIMIT ?", (limit,))
            return self._with_labels(rows)
        # Labels are JSON text: filter in Python, reading a bounded window of recent runs.
        rows = self._query(
            f"SELECT {cols} FROM {{runs}} ORDER BY started DESC LIMIT ?", (max(limit * 50, 1000),)
        )
        return self._with_labels(rows, labels)[:limit]

    def history(
        self, since: float, pipeline: str | None = None, labels: dict[str, str] | None = None
    ) -> list[dict]:
        """Every run started at or after `since` (epoch seconds), oldest first."""
        where = "started >= ?" + (" AND pipeline = ?" if pipeline else "")
        params: tuple = (since, pipeline) if pipeline else (since,)
        cols = "id, pipeline, status, outcome, started, finished, error, labels"
        rows = self._query(f"SELECT {cols} FROM {{runs}} WHERE {where} ORDER BY started", params)
        return self._with_labels(rows, labels)

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

    # evidence: the index of artifacts captured by source steps (#293)
    EVIDENCE_COLUMNS = (
        "run_id",
        "pipeline",
        "step",
        "url",
        "kind",
        "sha256",
        "bytes",
        "content_type",
        "captured_at",
        "location",
    )

    def evidence_add(self, row: dict) -> None:
        cols = self.EVIDENCE_COLUMNS
        self._exec(
            f"INSERT INTO {{evidence}} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            tuple(row.get(c) for c in cols),
        )

    def evidence_last(self, pipeline: str, step: str, url: str, kind: str, location: str) -> str | None:
        """The sha256 last indexed for this source in this storage location, or None."""
        rows = self._query(
            "SELECT sha256 FROM {evidence} WHERE pipeline = ? AND step = ? AND url = ? AND kind = ? "
            "AND location = ? ORDER BY id DESC LIMIT 1",
            (pipeline, step, url, kind, location),
        )
        return rows[0]["sha256"] if rows else None

    def evidence_list(
        self, run_id: str | None = None, sha256: str | None = None, limit: int = 1000
    ) -> list[dict]:
        """Indexed artifacts, newest first; `sha256` may be a prefix."""
        where, params = [], []
        if run_id:
            where.append("run_id = ?")
            params.append(run_id)
        if sha256:
            where.append("sha256 LIKE ?")
            params.append(sha256.replace("%", "").replace("_", "") + "%")
        sql = "SELECT * FROM {evidence}" + (" WHERE " + " AND ".join(where) if where else "")
        return self._query(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))

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

    # human review queue (#153): uncertain agent answers, and the answers people gave
    REVIEW_COLS: ClassVar[str] = (
        "id, run_id, pipeline, step, key, ts, reason, input, answer, status, value, reviewed"
    )
    REVIEW_STATUSES: ClassVar[tuple[str, ...]] = ("pending", "accepted", "fixed", "rejected")

    def _review_row(self, r: dict) -> dict:
        return {**r, **{k: self._load(r[k]) for k in ("input", "answer", "value")}}

    def review_add(
        self, run_id: str, pipeline: str, step: str, key: str, reason: str, record: dict, answer: dict
    ) -> bool:
        """Queue one answer. False when this input (key) is already queued or reviewed: the same
        input never asks a person twice."""
        found = self._query(
            "SELECT id FROM {review} WHERE pipeline = ? AND step = ? AND key = ? LIMIT 1",
            (pipeline, step, key),
        )
        if found:
            return False
        self._exec(
            "INSERT INTO {review} (run_id, pipeline, step, key, ts, reason, input, answer, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            (
                run_id,
                pipeline,
                step,
                key,
                time.time(),
                reason,
                json.dumps(record, default=str),
                json.dumps(answer, default=str),
            ),
        )
        return True

    def review_list(
        self,
        pipeline: str | None = None,
        step: str | None = None,
        status: str | None = "pending",
        limit: int | None = None,
    ) -> list[dict]:
        """Queued answers, oldest first; `status` None lists every status."""
        clauses = [
            c for c, v in (("pipeline = ?", pipeline), ("step = ?", step), ("status = ?", status)) if v
        ]
        params: list[Any] = [v for v in (pipeline, step, status) if v]
        sql = f"SELECT {self.REVIEW_COLS} FROM {{review}}"
        sql += (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [self._review_row(r) for r in self._query(sql, tuple(params))]

    def review_get(self, review_id: int) -> dict | None:
        rows = self._query(f"SELECT {self.REVIEW_COLS} FROM {{review}} WHERE id = ?", (review_id,))
        return self._review_row(rows[0]) if rows else None

    def review_resolve(self, review_id: int, status: str, value: dict | None) -> None:
        """Record a person's decision: accepted or fixed (with the answer to use) or rejected.
        Later runs read it through review_decisions, before the agent cache."""
        if status not in self.REVIEW_STATUSES[1:]:
            raise ValueError(f"review status must be accepted, fixed or rejected, not {status!r}")
        row = self.review_get(review_id)
        if row is None:
            raise KeyError(review_id)
        self._exec(
            "UPDATE {review} SET status = ?, value = ?, reviewed = ? WHERE id = ?",
            (status, None if value is None else json.dumps(value, default=str), time.time(), review_id),
        )

    def review_decisions(self, pipeline: str, step: str, keys: list[str]) -> dict[str, dict]:
        """{key: {status, value}} of the reviewed answers among `keys`."""
        found: dict[str, dict] = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i : i + 500]
            marks = ", ".join("?" * len(chunk))
            rows = self._query(
                f"SELECT key, status, value FROM {{review}} WHERE pipeline = ? AND step = ? "
                f"AND status != 'pending' AND key IN ({marks})",
                (pipeline, step, *chunk),
            )
            for r in rows:
                found[r["key"]] = {"status": r["status"], "value": self._load(r["value"])}
        return found

    def review_counts(self, pipeline: str | None = None) -> list[dict]:
        """[{pipeline, step, status, count}] over the whole queue."""
        where, params = (" WHERE pipeline = ?", (pipeline,)) if pipeline else ("", ())
        return self._query(
            f"SELECT pipeline, step, status, COUNT(*) AS count FROM {{review}}{where} "
            "GROUP BY pipeline, step, status ORDER BY pipeline, step, status",
            params,
        )

    def run(self, run_id: str) -> dict | None:
        rows = self._query("SELECT * FROM {runs} WHERE id = ?", (run_id,))
        return self._with_labels(rows)[0] if rows else None

    # run errors by fingerprint (#356), see agentl/errors.py --------------------
    def error_seen(
        self, pipeline: str, fingerprint: str, step: str | None, error_class: str, error: str, at: float
    ) -> dict:
        """Count one occurrence of an error and return its row. A resolved error that comes back
        opens again with a fresh count, first seen and alert."""
        found = self.errors_of(pipeline, fingerprint)
        if found and found[0]["status"] == "open":
            self._exec(
                "UPDATE {errors} SET count = count + 1, last_seen = ?, error = ?, step = ? "
                "WHERE pipeline = ? AND fingerprint = ?",
                (at, error, step, pipeline, fingerprint),
            )
        elif found:
            self._exec(
                "UPDATE {errors} SET count = 1, first_seen = ?, last_seen = ?, error = ?, step = ?, "
                "status = 'open', alerted = NULL, resolved = NULL WHERE pipeline = ? AND fingerprint = ?",
                (at, at, error, step, pipeline, fingerprint),
            )
        else:
            self._exec(
                "INSERT INTO {errors} (pipeline, fingerprint, step, error_class, error, first_seen, "
                "last_seen, count, status) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'open')",
                (pipeline, fingerprint, step, error_class, error, at, at),
            )
        return self.errors_of(pipeline, fingerprint)[0]

    def error_alerted(self, pipeline: str, fingerprint: str, at: float) -> None:
        self._exec(
            "UPDATE {errors} SET alerted = ? WHERE pipeline = ? AND fingerprint = ?",
            (at, pipeline, fingerprint),
        )

    def errors_resolve(self, pipeline: str, at: float) -> list[dict]:
        """Mark the pipeline's open errors resolved; returns them as they were."""
        rows = self._query(
            "SELECT * FROM {errors} WHERE pipeline = ? AND status = 'open' ORDER BY first_seen",
            (pipeline,),
        )
        if rows:
            self._exec(
                "UPDATE {errors} SET status = 'resolved', resolved = ? "
                "WHERE pipeline = ? AND status = 'open'",
                (at, pipeline),
            )
        return rows

    def errors_of(
        self, pipeline: str, fingerprint: str | None = None, resolved_since: float | None = None
    ) -> list[dict]:
        """One error by fingerprint, or the pipeline's open errors plus those resolved at or after
        `resolved_since` (all resolved ones when None), oldest first."""
        if fingerprint is not None:
            return self._query(
                "SELECT * FROM {errors} WHERE pipeline = ? AND fingerprint = ?", (pipeline, fingerprint)
            )
        return self._query(
            "SELECT * FROM {errors} WHERE pipeline = ? AND (status = 'open' OR resolved >= ?) "
            "ORDER BY first_seen",
            (pipeline, resolved_since if resolved_since is not None else float("-inf")),
        )

    def events(self, run_id: str) -> list[dict]:
        rows = self._query(
            "SELECT ts, type, step, data FROM {events} WHERE run_id = ? ORDER BY id", (run_id,)
        )
        return [{**r, "data": self._load(r["data"])} for r in rows]


class PostgresStore(Store):
    """Postgres store, shared by every worker that points to the same database.
    Needs `agentl-etl[postgres]`. Tables: agentl_runs, agentl_events, agentl_cache, agentl_state,
    agentl_samples, agentl_rejected, agentl_locks, agentl_seen, agentl_review, agentl_errors,
    agentl_evidence."""

    T: ClassVar[dict[str, str]] = {
        "runs": "agentl_runs",
        "events": "agentl_events",
        "cache": "agentl_cache",
        "state": "agentl_state",
        "samples": "agentl_samples",
        "rejected": "agentl_rejected",
        "locks": "agentl_locks",
        "seen": "agentl_seen",
        "review": "agentl_review",
        "errors": "agentl_errors",
        "evidence": "agentl_evidence",
        "schema_version": "agentl_schema_version",
    }
    INSERT_SEEN = (
        "INSERT INTO {seen} (scope, key, run_id, ts) VALUES (?, ?, ?, ?) ON CONFLICT (scope, key) DO NOTHING"
    )
    UPSERT_CACHE = (
        "INSERT INTO {cache} (key, value, ts, expires) VALUES (?, ?::jsonb, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, ts = EXCLUDED.ts, expires = EXCLUDED.expires"
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
        # Workers may start together: an advisory lock serializes the schema setup.
        with self._conn.transaction():
            self._conn.execute("SELECT pg_advisory_xact_lock(hashtext('agentl_schema'))")
            for stmt in POSTGRES_SCHEMA:
                self._conn.execute(stmt)
            self._migrate()

    def _add_column(self, table: str, column: str, sqlite_type: str, pg_type: str) -> None:
        self._conn.execute(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{column}" {pg_type}')

    def _sql(self, sql: str) -> str:
        return super()._sql(sql).replace("?", "%s")

    def _exec(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            return int(self._conn.execute(self._sql(sql), params).rowcount)

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return list(self._conn.execute(self._sql(sql), params).fetchall())

    def cache_get(self, key: str) -> Any | None:
        """As in SQLite. The JSONB column comes back decoded, so a cached string (the digest's
        sent-today guard) is not parsed a second time."""
        rows = self._query(
            "SELECT value FROM {cache} WHERE key = ? AND (expires IS NULL OR expires > ?)", (key, time.time())
        )
        return rows[0]["value"] if rows else None

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
