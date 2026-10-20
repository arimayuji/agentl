"""Stores created by older releases open, migrate to the current schema and keep their data."""

import json
import os
import sqlite3
import uuid
from urllib.parse import quote

import pytest

from agentl.store import SCHEMA_VERSION, PostgresStore, Store

DSN = os.environ.get("AGENTL_TEST_PG_DSN")

# CREATE TABLE statements of agentl 0.11.0 (git show v0.11.0:agentl/store.py): no seen, locks or
# review tables, no runs.outcome, runs.labels or cache.expires columns, no schema version.
SQLITE_0_11 = """
CREATE TABLE runs (
  id TEXT PRIMARY KEY, pipeline TEXT, status TEXT, started REAL, finished REAL,
  error TEXT, diagram TEXT
);
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts REAL, type TEXT,
  step TEXT, data TEXT
);
CREATE INDEX events_run ON events(run_id);
CREATE TABLE cache (key TEXT PRIMARY KEY, value TEXT, ts REAL);
CREATE TABLE state (
  pipeline TEXT, step TEXT, value TEXT, run_id TEXT, ts REAL, PRIMARY KEY (pipeline, step)
);
CREATE TABLE samples (
  run_id TEXT, step TEXT, records TEXT, total INTEGER, PRIMARY KEY (run_id, step)
);
CREATE TABLE rejected (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, pipeline TEXT, step TEXT, ts REAL,
  error TEXT, record TEXT, retried_by TEXT
);
"""

# agentl 0.12.0 (git show v0.12.0:agentl/store.py): the seen table exists and the ad hoc
# ALTER TABLE calls had already added the columns, but there is no schema version.
SQLITE_0_12 = """
CREATE TABLE runs (
  id TEXT PRIMARY KEY, pipeline TEXT, status TEXT, started REAL, finished REAL,
  error TEXT, diagram TEXT, outcome TEXT, labels TEXT
);
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts REAL, type TEXT,
  step TEXT, data TEXT
);
CREATE TABLE cache (key TEXT PRIMARY KEY, value TEXT, ts REAL, expires REAL);
CREATE TABLE locks (key TEXT PRIMARY KEY, owner TEXT, expires REAL);
CREATE TABLE state (
  pipeline TEXT, step TEXT, value TEXT, run_id TEXT, ts REAL, PRIMARY KEY (pipeline, step)
);
CREATE TABLE samples (
  run_id TEXT, step TEXT, records TEXT, total INTEGER, PRIMARY KEY (run_id, step)
);
CREATE TABLE rejected (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, pipeline TEXT, step TEXT, ts REAL,
  error TEXT, record TEXT, retried_by TEXT
);
CREATE TABLE seen (
  scope TEXT, key TEXT, run_id TEXT, ts REAL, PRIMARY KEY (scope, key)
);
"""

POSTGRES_0_11 = [
    """CREATE TABLE agentl_runs (
      id TEXT PRIMARY KEY, pipeline TEXT, status TEXT, started DOUBLE PRECISION,
      finished DOUBLE PRECISION, error TEXT, diagram TEXT)""",
    """CREATE TABLE agentl_events (
      id BIGSERIAL PRIMARY KEY, run_id TEXT, ts DOUBLE PRECISION, type TEXT, step TEXT, data JSONB)""",
    "CREATE TABLE agentl_cache (key TEXT PRIMARY KEY, value JSONB, ts DOUBLE PRECISION)",
    """CREATE TABLE agentl_state (
      pipeline TEXT, step TEXT, value JSONB, run_id TEXT, ts DOUBLE PRECISION,
      PRIMARY KEY (pipeline, step))""",
    """CREATE TABLE agentl_samples (
      run_id TEXT, step TEXT, records JSONB, total INTEGER, PRIMARY KEY (run_id, step))""",
    """CREATE TABLE agentl_rejected (
      id BIGSERIAL PRIMARY KEY, run_id TEXT, pipeline TEXT, step TEXT, ts DOUBLE PRECISION,
      error TEXT, record JSONB, retried_by TEXT)""",
]


def fill_sqlite(path, schema, seen):
    conn = sqlite3.connect(path)
    conn.executescript(schema)
    conn.execute("INSERT INTO runs (id, pipeline, status, started, finished) VALUES ('r1', 'p', 'ok', 1, 2)")
    conn.execute("INSERT INTO cache (key, value, ts) VALUES ('k', ?, 1)", (json.dumps({"a": 1}),))
    conn.execute("INSERT INTO state VALUES ('p', 'src', ?, 'r1', 1)", (json.dumps({"v": {"cursor": 7}}),))
    if seen:
        conn.execute("INSERT INTO seen VALUES ('p/src', 'id-1', 'r1', 1)")
    conn.commit()
    conn.close()


def columns(path, table):
    conn = sqlite3.connect(path)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def check_data(s, seen):
    assert s.schema_version() == SCHEMA_VERSION
    assert s.cache_get("k") == {"a": 1}
    assert s.state_get("p", "src") == {"cursor": 7}
    [run] = s.runs()
    assert (run["id"], run["status"], run["outcome"], run["labels"]) == ("r1", "ok", None, {})
    if seen:
        assert s.seen_filter("p/src", ["id-1", "id-2"]) == {"id-1"}
    # The migrated store takes new writes in the added columns and tables.
    s.cache_set("k2", [1], ttl=60)
    assert s.cache_get("k2") == [1]
    s.seen_add("p/src", ["id-3"], "r2")
    assert s.seen_filter("p/src", ["id-3"]) == {"id-3"}


@pytest.mark.parametrize(
    ("schema", "seen"), [(SQLITE_0_11, False), (SQLITE_0_12, True)], ids=["0.11", "0.12"]
)
def test_sqlite_old_store_migrates(tmp_path, schema, seen):
    path = tmp_path / "old.db"
    fill_sqlite(path, schema, seen)
    with Store(path) as s:
        check_data(s, seen)
    assert {"outcome", "labels"} <= columns(path, "runs")
    assert "expires" in columns(path, "cache")
    before = sqlite3.connect(path).execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    with Store(path) as s:  # second open: nothing left to migrate
        assert s.schema_version() == SCHEMA_VERSION
        assert s.cache_get("k2") == [1]
    after = sqlite3.connect(path).execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    assert before == after


def test_sqlite_fresh_store_is_current(tmp_path):
    with Store(tmp_path / "new.db") as s:
        assert s.schema_version() == SCHEMA_VERSION
    with Store(tmp_path / "new.db") as s:
        assert s._query("SELECT COUNT(*) AS n FROM {schema_version}")[0]["n"] == 1


def test_sqlite_newer_store_is_not_downgraded(tmp_path):
    path = tmp_path / "newer.db"
    with Store(path):
        pass
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION + 5,))
    conn.commit()
    conn.close()
    with Store(path) as s:
        assert s.schema_version() == SCHEMA_VERSION + 5


@pytest.fixture
def pg_schema():
    if not DSN:
        pytest.skip("set AGENTL_TEST_PG_DSN to run Postgres tests")
    psycopg = pytest.importorskip("psycopg")
    name = f"agentl_old_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{name}"')
    sep = "&" if "?" in DSN else "?"
    yield name, f"{DSN}{sep}options={quote(f'-csearch_path={name}')}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA "{name}" CASCADE')


def test_postgres_old_store_migrates(pg_schema):
    psycopg = pytest.importorskip("psycopg")
    name, dsn = pg_schema
    with psycopg.connect(dsn, autocommit=True) as conn:
        for stmt in POSTGRES_0_11:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO agentl_runs (id, pipeline, status, started, finished) VALUES ('r1', 'p', 'ok', 1, 2)"
        )
        conn.execute(
            "INSERT INTO agentl_cache (key, value, ts) VALUES ('k', %s::jsonb, 1)", (json.dumps({"a": 1}),)
        )
        conn.execute(
            "INSERT INTO agentl_state VALUES ('p', 'src', %s::jsonb, 'r1', 1)",
            (json.dumps({"v": {"cursor": 7}}),),
        )
    with PostgresStore(dsn) as s:
        check_data(s, seen=False)

    def layout():
        with psycopg.connect(DSN) as conn:
            return conn.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = %s ORDER BY 1, 2",
                (name,),
            ).fetchall()

    before = layout()
    assert ("agentl_runs", "labels", "text") in before
    assert ("agentl_cache", "expires", "double precision") in before
    with PostgresStore(dsn) as s:
        assert s.schema_version() == SCHEMA_VERSION
        assert s._query("SELECT COUNT(*) AS n FROM {schema_version}")[0]["n"] == 1
        assert s.cache_get("k") == {"a": 1}
    assert layout() == before


def _use_errors_table(s):
    row = s.error_seen("p", "agentl-err-1", "src", "RuntimeError", "RuntimeError: boom", 10.0)
    assert (row["count"], row["status"]) == (1, "open")
    assert [r["fingerprint"] for r in s.errors_resolve("p", 20.0)] == ["agentl-err-1"]


def test_sqlite_version_3_store_gains_the_errors_table(tmp_path):
    path = tmp_path / "v3.db"
    with Store(path):
        pass
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE errors")  # the layout agentl had at schema version 3
    conn.execute("UPDATE schema_version SET version = 3")
    conn.commit()
    conn.close()
    with Store(path) as s:
        assert s.schema_version() == SCHEMA_VERSION == 4
        assert s.cache_get("missing") is None
        _use_errors_table(s)
    assert {"fingerprint", "count", "alerted", "resolved"} <= columns(path, "errors")


def test_postgres_version_3_store_gains_the_errors_table(pg_schema):
    psycopg = pytest.importorskip("psycopg")
    _, dsn = pg_schema
    with PostgresStore(dsn):
        pass
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE agentl_errors")
        conn.execute("UPDATE agentl_schema_version SET version = 3")
    with PostgresStore(dsn) as s:
        assert s.schema_version() == SCHEMA_VERSION == 4
        _use_errors_table(s)
