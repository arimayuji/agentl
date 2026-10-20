"""mode: changeset on sqlite.write (#205): diff with the table, deletes, partitions, dry run."""

import json
import sqlite3

import pytest

from agentl import parse
from agentl.connectors.sql import sqlite_write_chunks
from agentl.registry import get
from agentl.runner import Runner
from agentl.store import Store

ROWS = [{"id": i, "month": "2026-0" + str(1 + i % 2), "v": f"v{i}"} for i in range(6)]


def table(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("select id, month, v from t order by id").fetchall()


def write(db, data, **kw):
    return get("sqlite.write")(data, db=db, table="t", mode="changeset", key="id", **kw)[0]


def counts(out):
    return tuple(out[k] for k in ("inserted", "updated", "deleted", "unchanged"))


def test_same_data_is_no_change_and_zero_writes(tmp_path):
    db = str(tmp_path / "x.db")
    assert counts(write(db, ROWS)) == (6, 0, 0, 0)
    with sqlite3.connect(db) as conn:  # a versioned table: every write leaves a row in audit
        conn.execute("create table audit (id)")
        for op in ("insert", "update", "delete"):
            conn.execute(f"create trigger t_{op} after {op} on t begin insert into audit values (1); end")
    again = write(db, ROWS, deletes="missing")
    assert counts(again) == (0, 0, 0, 6)
    assert again["outcome"] == "no_change" and again["written"] == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("select count(*) from audit").fetchone() == (0,)


def test_updates_inserts_and_deletes_missing(tmp_path):
    db = str(tmp_path / "x.db")
    write(db, ROWS)
    new = [*ROWS[:3], {"id": 3, "month": "2026-02", "v": "changed"}, {"id": 9, "month": "2026-03", "v": "n"}]
    out = write(db, new, deletes="missing")
    assert counts(out) == (1, 1, 2, 3) and out["outcome"] == "changed" and out["written"] == 4
    assert [r[0] for r in table(db)] == [0, 1, 2, 3, 9]


def test_partition_replaced_deletes_only_in_source_partitions(tmp_path):
    db = str(tmp_path / "x.db")
    write(db, ROWS)
    jan = [r for r in ROWS if r["month"] == "2026-01"][:1]  # id 0; ids 2 and 4 left the partition
    out = write(db, jan, partition_by="month", deletes={"when": "partition_replaced"})
    assert counts(out) == (0, 0, 2, 1)
    assert (out["partitions"], out["partitions_unchanged"]) == (1, 0)
    assert [r[0] for r in table(db)] == [0, 1, 3, 5]


def test_partition_counts_skip_equal_partitions(tmp_path):
    db = str(tmp_path / "x.db")
    write(db, ROWS)
    new = [dict(r, v="x") if r["id"] == 1 else r for r in ROWS]
    out = write(db, new, partition_by="month")
    assert counts(out) == (0, 1, 0, 5)
    assert (out["partitions"], out["partitions_unchanged"]) == (2, 1)


def test_dry_run_reports_without_writing(tmp_path):
    db = str(tmp_path / "x.db")
    write(db, ROWS)
    new = [{"id": 0, "month": "2026-01", "v": "x", "extra": 1}, {"id": 7, "month": "2026-01", "v": "n"}]
    out = write(db, new, deletes="missing", dry_run=True)
    assert counts(out) == (1, 1, 5, 0) and out["dry_run"] is True and out["written"] == 0
    assert table(db) == [(r["id"], r["month"], r["v"]) for r in ROWS]
    with sqlite3.connect(db) as conn:
        assert "extra" not in [c[1] for c in conn.execute("pragma table_info(t)")]


def test_dry_run_on_missing_table_creates_nothing(tmp_path):
    db = str(tmp_path / "x.db")
    sqlite3.connect(db).close()
    assert counts(write(db, ROWS, dry_run=True)) == (6, 0, 0, 0)
    with sqlite3.connect(db) as conn:
        assert conn.execute("select count(*) from sqlite_master").fetchone()[0] == 0


def test_new_column_counts_as_update_and_widens_table(tmp_path):
    db = str(tmp_path / "x.db")
    write(db, ROWS)
    out = write(db, [dict(ROWS[0], extra=1), *ROWS[1:]])
    assert counts(out) == (0, 1, 0, 5)
    with sqlite3.connect(db) as conn:
        assert conn.execute("select extra from t where id = 0").fetchone() == (1,)


def test_chunks_are_diffed_as_one_source(tmp_path):
    db = str(tmp_path / "x.db")
    write(db, ROWS)
    out = sqlite_write_chunks(
        [ROWS[:2], ROWS[2:4]], db=db, table="t", mode="changeset", key="id", deletes="missing"
    )[0]
    assert counts(out) == (0, 0, 2, 4)


def test_last_record_of_a_key_wins(tmp_path):
    db = str(tmp_path / "x.db")
    out = write(db, [{"id": 1, "month": "m", "v": "a"}, {"id": 1, "month": "m", "v": "b"}])
    assert counts(out) == (1, 0, 0, 0) and table(db) == [(1, "m", "b")]


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"mode": "upsert", "deletes": "missing"}, "need mode: changeset"),
        ({"mode": "append", "dry_run": True}, "need mode: changeset"),
        ({"key": None}, "needs `key`"),
        ({"deletes": "all"}, "deletes must be one of"),
        ({"deletes": {"when": "missing", "x": 1}}, "only `when`"),
        ({"deletes": "partition_replaced"}, "needs partition_by"),
    ],
)
def test_options_are_validated(tmp_path, kw, match):
    args = {"mode": "changeset", "key": "id", **kw}
    with pytest.raises(ValueError, match=match):
        get("sqlite.write")(ROWS, db=str(tmp_path / "x.db"), table="t", **args)


def test_counts_in_events_and_run_summary(tmp_path):
    db = str(tmp_path / "x.db")
    write(db, ROWS)
    (tmp_path / "in.json").write_text(json.dumps(ROWS[:5]))
    spec = parse(
        {
            "name": "changeset",
            "steps": [
                {"id": "src", "use": "json.read", "with": {"path": str(tmp_path / "in.json")}},
                {
                    "id": "save",
                    "use": "sqlite.write",
                    "input": "src",
                    "with": {"db": db, "table": "t", "mode": "changeset", "key": "id", "deletes": "missing"},
                },
            ],
        }
    )
    with Store(tmp_path / "state.db") as store:
        result = Runner(spec, store=store).run()
        events = store.events(result.run_id)
    assert result.status == "succeeded", result.error
    done = next(e for e in events if e["type"] == "step.succeeded" and e["step"] == "save")
    assert done["data"]["deleted"] == 1 and done["data"]["written"] == 1
    assert result.summary()["writes"] == {"save": {"inserted": 0, "updated": 0, "deleted": 1, "unchanged": 5}}


def test_null_keys_are_rejected_as_in_upsert(tmp_path):
    db = str(tmp_path / "c.db")
    out = get("sqlite.write")(
        [{"id": None, "month": "2026-01", "v": "x"}, *ROWS[:2]], db=db, table="t", mode="changeset", key="id"
    )
    assert out[0]["inserted"] == 2
    assert len(out.rejected) == 1 and "is null" in out.rejected[0]["_error"]
    assert [r[0] for r in table(db)] == [0, 1]


@pytest.mark.parametrize("chunk_size", [None, 2])
def test_emit_changes_feeds_the_next_step_and_keeps_counts(tmp_path, chunk_size):
    db = str(tmp_path / "x.db")
    write(db, ROWS)
    changed = [dict(r, v="new") if r["id"] == 2 else r for r in ROWS[:5]]
    src = str(tmp_path / "src.db")
    get("sqlite.write")(changed, db=src, table="src")  # sqlite.read streams, so chunk_size chains it
    out = tmp_path / "changes.json"
    spec = parse(
        {
            "name": "changes",
            "chunk_size": chunk_size,
            "steps": [
                {"id": "src", "use": "sqlite.read", "with": {"db": src, "query": "select * from src"}},
                {
                    "id": "save",
                    "use": "sqlite.write",
                    "input": "src",
                    "with": {
                        "db": db,
                        "table": "t",
                        "mode": "changeset",
                        "key": "id",
                        "deletes": "missing",
                        "emit": "changes",
                    },
                },
                {"id": "log", "use": "json.write", "input": "save", "with": {"path": str(out)}},
            ],
        }
    )
    with Store(tmp_path / "state.db") as store:
        result = Runner(spec, store=store).run()
        events = store.events(result.run_id)
    assert result.status == "succeeded", result.error
    assert result.summary()["writes"]["save"] == {"inserted": 0, "updated": 1, "deleted": 1, "unchanged": 4}
    done = next(e for e in events if e["type"] == "step.succeeded" and e["step"] == "save")
    assert done["data"]["written"] == 2 and done["data"]["deleted"] == 1
    assert done["data"].get("chunked", False) is (chunk_size is not None)
    rows = json.loads(out.read_text())
    assert [(r["_change"], r["id"]) for r in rows] == [("updated", 2), ("deleted", 5)]
    assert rows[0]["_before"] == {"v": "v2"}
