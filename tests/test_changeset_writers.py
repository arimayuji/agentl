"""mode: changeset on every SQL writer (#205): sqlite.write, postgres.write and sql.write share
the semantics, and partition checksums skip equal partitions."""

import os
import sqlite3
import uuid

import pytest

from agentl.registry import get

DSN = os.environ.get("AGENTL_TEST_PG_DSN")
ROWS = [{"id": i, "month": "2026-0" + str(1 + i % 2), "v": f"v{i}"} for i in range(6)]


class Backend:
    """One writer under test: `write` calls it in changeset mode, `rows` reads the table back."""

    def __init__(self, name, tmp_path):
        self.name = name
        self.table = f"t_{uuid.uuid4().hex[:8]}"
        self.db = str(tmp_path / "x.db")
        if name == "sqlite":
            self.args = {"db": self.db}
        elif name == "sql":
            pytest.importorskip("sqlalchemy")
            self.args = {"url": f"sqlite:///{self.db}"}
        elif name == "sql-postgres":
            pytest.importorskip("sqlalchemy")
            self.args = {"url": DSN.replace("postgresql://", "postgresql+psycopg://", 1)}
        else:
            self.args = {"dsn": DSN}

    @property
    def use(self):
        return {"sqlite": "sqlite.write", "postgres": "postgres.write"}.get(self.name, "sql.write")

    def write(self, data, **kw):
        kw.setdefault("key", "id")
        return get(self.use)(data, table=self.table, mode="changeset", **self.args, **kw)

    def query(self, q):
        if self.name in ("sqlite", "sql"):
            with sqlite3.connect(self.db) as conn:
                return conn.execute(q).fetchall()
        psycopg = pytest.importorskip("psycopg")
        with psycopg.connect(DSN) as conn:
            return conn.execute(q).fetchall()

    def exists(self):
        if self.name in ("sqlite", "sql"):
            return bool(self.query(f"select 1 from sqlite_master where name = '{self.table}'"))
        return self.query(f"select to_regclass('\"{self.table}\"') is not null")[0][0]

    def rows(self, cols="id, month, v"):
        return [tuple(r) for r in self.query(f'select {cols} from "{self.table}" order by id')]

    def columns(self):
        if self.name in ("sqlite", "sql"):
            return [c[1] for c in self.query(f'pragma table_info("{self.table}")')]
        return [
            c[0]
            for c in self.query(
                f"select column_name from information_schema.columns where table_name = '{self.table}'"
            )
        ]

    def drop(self):
        if self.name in ("postgres", "sql-postgres"):
            psycopg = pytest.importorskip("psycopg")
            with psycopg.connect(DSN) as conn:
                conn.execute(f'DROP TABLE IF EXISTS "{self.table}"')
                if conn.execute("select to_regclass('_agentl_checksums')").fetchone()[0]:
                    conn.execute("DELETE FROM _agentl_checksums WHERE tbl = %s", [self.table])


needs_pg = pytest.mark.skipif(not DSN, reason="set AGENTL_TEST_PG_DSN to run Postgres tests")


@pytest.fixture(
    params=[
        "sqlite",
        "sql",
        pytest.param("postgres", marks=needs_pg),
        pytest.param("sql-postgres", marks=needs_pg),
    ]
)
def b(request, tmp_path):
    backend = Backend(request.param, tmp_path)
    yield backend
    backend.drop()


def counts(out):
    out = out[0]
    return tuple(out[k] for k in ("inserted", "updated", "deleted", "unchanged"))


def test_same_data_is_no_change(b):
    assert counts(b.write(ROWS)) == (6, 0, 0, 0)
    again = b.write(ROWS, deletes="missing")[0]
    assert (again["inserted"], again["updated"], again["deleted"], again["unchanged"]) == (0, 0, 0, 6)
    assert again["outcome"] == "no_change" and again["written"] == 0


def test_inserts_updates_and_deletes_missing(b):
    b.write(ROWS)
    new = [*ROWS[:3], {"id": 3, "month": "2026-02", "v": "changed"}, {"id": 9, "month": "2026-03", "v": "n"}]
    out = b.write(new, deletes="missing")
    assert counts(out) == (1, 1, 2, 3) and out[0]["outcome"] == "changed" and out[0]["written"] == 4
    assert [r[0] for r in b.rows()] == [0, 1, 2, 3, 9]
    assert b.rows()[3] == (3, "2026-02", "changed")


def test_partition_replaced_deletes_only_in_source_partitions(b):
    b.write(ROWS)
    out = b.write(ROWS[:1], partition_by="month", deletes={"when": "partition_replaced"})[0]
    assert (out["inserted"], out["updated"], out["deleted"], out["unchanged"]) == (0, 0, 2, 1)
    assert (out["partitions"], out["partitions_unchanged"]) == (1, 0)
    assert [r[0] for r in b.rows()] == [0, 1, 3, 5]


def test_partition_counts(b):
    b.write(ROWS)
    out = b.write([dict(r, v="x") if r["id"] == 1 else r for r in ROWS], partition_by="month")
    assert counts(out) == (0, 1, 0, 5)
    assert (out[0]["partitions"], out[0]["partitions_unchanged"]) == (2, 1)


def test_dry_run_writes_nothing(b):
    out = b.write(ROWS, dry_run=True)
    assert counts(out) == (6, 0, 0, 0) and out[0]["dry_run"] is True and out[0]["written"] == 0
    assert not b.exists()
    b.write(ROWS)
    new = [{"id": 0, "month": "2026-01", "v": "x", "extra": 1}, {"id": 7, "month": "2026-01", "v": "n"}]
    assert counts(b.write(new, deletes="missing", dry_run=True)) == (1, 1, 5, 0)
    assert b.rows() == [(r["id"], r["month"], r["v"]) for r in ROWS]
    assert "extra" not in b.columns()


def test_new_column_counts_as_update_and_widens_table(b):
    b.write(ROWS)
    assert counts(b.write([dict(ROWS[0], extra=1), *ROWS[1:]])) == (0, 1, 0, 5)
    assert b.rows("id, extra")[0] == (0, 1)


def test_last_record_of_a_key_wins_and_null_keys_are_rejected(b):
    out = b.write(
        [{"id": 1, "month": "m", "v": "a"}, {"id": 1, "month": "m", "v": "b"}, {"id": None, "v": "z"}]
    )
    assert counts(out) == (1, 0, 0, 0) and b.rows() == [(1, "m", "b")]
    assert len(out.rejected) == 1 and "is null" in out.rejected[0]["_error"]


def test_null_values_compare_equal(b):
    rows = [{"id": 1, "month": "m", "v": None}]
    b.write(rows)
    assert counts(b.write(rows)) == (0, 0, 0, 1)
    assert counts(b.write([{"id": 1, "month": "m", "v": "now"}])) == (0, 1, 0, 0)


def test_checksums_skip_equal_partitions(b):
    first = b.write(ROWS, partition_by="month", checksums=True)[0]
    assert first["partitions_skipped"] == 0 and first["inserted"] == 6
    again = b.write(ROWS, partition_by="month", checksums=True, deletes="missing")[0]
    assert (again["partitions"], again["partitions_unchanged"], again["partitions_skipped"]) == (2, 2, 2)
    assert again["outcome"] == "no_change" and again["unchanged"] == 6
    new = [dict(r, v="x") if r["id"] == 1 else r for r in ROWS]  # 2026-02 changes, 2026-01 is equal
    out = b.write(new, partition_by="month", checksums=True, deletes="missing")[0]
    assert out["partitions_skipped"] == 1 and (out["updated"], out["unchanged"]) == (1, 5)
    assert b.rows()[1] == (1, "2026-02", "x")


def test_checksums_keep_rows_of_skipped_partitions_with_deletes_missing(b):
    b.write(ROWS, partition_by="month", checksums=True)
    b.write([{"id": 99, "month": "2026-01", "v": "other"}], key="id")  # a row the source never had
    out = b.write(ROWS, partition_by="month", checksums=True, deletes="missing")[0]
    assert out["partitions_skipped"] == 2 and out["deleted"] == 0
    assert 99 in [r[0] for r in b.rows()]


def test_checksums_are_not_stored_on_dry_run(b):
    b.write(ROWS, partition_by="month", checksums=True, dry_run=True)
    b.write(ROWS)
    out = b.write(ROWS, partition_by="month", checksums=True)[0]
    assert out["partitions_skipped"] == 0 and out["unchanged"] == 6


def test_checksums_need_partition_by(b):
    with pytest.raises(ValueError, match="checksums needs partition_by"):
        b.write(ROWS, checksums=True)
    with pytest.raises(ValueError, match="need mode: changeset"):
        get(b.use)(ROWS, table=b.table, mode="upsert", key="id", checksums=True, **b.args)


def test_date_partitions_match_values_read_back(b):
    import datetime as dt

    rows = [{"id": i, "day": dt.date(2026, 1, 1 + i % 2), "v": f"v{i}"} for i in range(4)]
    b.write(rows, partition_by="day")
    out = b.write(rows[:1], partition_by="day", deletes="partition_replaced", checksums=True)[0]
    assert (out["deleted"], out["unchanged"], out["partitions"]) == (1, 1, 1)
    assert [r[0] for r in b.rows("id")] == [0, 1, 3]


def changes(out):
    return sorted(((r["_change"], r["id"]) for r in out), key=lambda c: (c[0], c[1]))


def test_emit_changes_returns_the_changed_records(b):
    first = b.write(ROWS[:2], emit="changes")
    assert changes(first) == [("inserted", 0), ("inserted", 1)]
    assert first.summary[0]["inserted"] == 2
    new = [{"id": 0, "month": "2026-01", "v": "x"}, {"id": 7, "month": "2026-02", "v": "n"}]
    out = b.write(new, deletes="missing", emit="changes")
    assert changes(out) == [("deleted", 1), ("inserted", 7), ("updated", 0)]
    upd = next(r for r in out if r["_change"] == "updated")
    assert upd["v"] == "x" and upd["_before"] == {"v": "v0"}
    gone = next(r for r in out if r["_change"] == "deleted")
    assert (gone["month"], gone["v"]) == ("2026-02", "v1")
    assert (out.summary[0]["written"], out.summary[0]["deleted"]) == (3, 1)


def test_emit_changes_is_empty_without_change_and_works_on_dry_run(b):
    b.write(ROWS)
    assert list(b.write(ROWS, emit="changes")) == []
    preview = b.write([dict(ROWS[0], v="y")], emit="changes", dry_run=True)
    assert changes(preview) == [("updated", 0)] and preview[0]["_before"] == {"v": "v0"}
    assert b.rows()[0] == (0, "2026-01", "v0")


def test_emit_changes_reports_new_columns_in_before(b):
    b.write(ROWS[:1])
    out = b.write([dict(ROWS[0], extra=5)], emit="changes")
    assert out[0]["_before"] == {"extra": None} and out[0]["extra"] == 5


def test_emit_is_validated(b):
    with pytest.raises(ValueError, match="emit must be counts or changes"):
        b.write(ROWS, emit="rows")
    with pytest.raises(ValueError, match="emit: changes needs mode: changeset"):
        get(b.use)(ROWS, table=b.table, mode="upsert", key="id", emit="changes", **b.args)
