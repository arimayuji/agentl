"""Upsert options and counts (#183) on sqlite.write; the Postgres cases live in test_postgres.py."""

import json
import sqlite3

import pytest

from agentl import parse
from agentl.registry import get
from agentl.runner import Runner
from agentl.store import Store

ROWS = [{"id": 1, "v": "a", "n": 1}, {"id": 2, "v": "b", "n": 2}, {"id": 3, "v": "c", "n": 3}]


def rows(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("select id, v, n from t order by id").fetchall()


def write(db, data, **kw):
    return get("sqlite.write")(data, db=db, table="t", mode="upsert", key="id", **kw)[0]


def test_rerun_leaves_table_unchanged(tmp_path):
    db = str(tmp_path / "x.db")
    first = write(db, ROWS)
    assert (first["inserted"], first["updated"], first["unchanged"]) == (3, 0, 0)
    before = rows(db)
    again = write(db, ROWS)
    assert again == {"table": "t", "written": 3, "inserted": 0, "updated": 0, "unchanged": 3}
    assert rows(db) == before


def test_update_counts_only_changed_rows(tmp_path):
    db = str(tmp_path / "x.db")
    write(db, ROWS)
    out = write(db, [{"id": 1, "v": "a", "n": 1}, {"id": 2, "v": "B", "n": 2}, {"id": 4, "v": "d", "n": 4}])
    assert (out["inserted"], out["updated"], out["unchanged"]) == (1, 1, 1)
    assert rows(db) == [(1, "a", 1), (2, "B", 2), (3, "c", 3), (4, "d", 4)]


def test_do_nothing_keeps_existing_rows(tmp_path):
    db = str(tmp_path / "x.db")
    write(db, ROWS)
    out = write(db, [{"id": 1, "v": "X", "n": 9}, {"id": 5, "v": "e", "n": 5}], on_conflict="do_nothing")
    assert (out["inserted"], out["updated"], out["unchanged"]) == (1, 0, 1)
    assert rows(db)[0] == (1, "a", 1) and rows(db)[-1] == (5, "e", 5)


def test_error_fails_on_existing_key_and_rolls_back(tmp_path):
    db = str(tmp_path / "x.db")
    write(db, ROWS)
    with pytest.raises(ValueError, match="on_conflict: error"):
        write(db, [{"id": 9, "v": "z", "n": 0}, {"id": 1, "v": "X", "n": 9}], on_conflict="error")
    assert rows(db) == [(1, "a", 1), (2, "b", 2), (3, "c", 3)]
    assert write(db, [{"id": 9, "v": "z", "n": 0}], on_conflict="error")["inserted"] == 1


def test_update_columns_updates_only_those(tmp_path):
    db = str(tmp_path / "x.db")
    write(db, ROWS)
    out = write(db, [{"id": 1, "v": "X", "n": 1}, {"id": 2, "v": "Y", "n": 20}], update_columns=["n"])
    assert (out["inserted"], out["updated"], out["unchanged"]) == (0, 1, 1)
    assert rows(db)[:2] == [(1, "a", 1), (2, "b", 20)]


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"mode": "append", "on_conflict": "do_nothing"}, "need mode: upsert"),
        ({"mode": "append", "update_columns": ["v"]}, "need mode: upsert"),
        ({"on_conflict": "merge"}, "on_conflict must be one of"),
        ({"on_conflict": "do_nothing", "update_columns": ["v"]}, "only applies to on_conflict: update"),
        ({"update_columns": ["id"]}, "cannot include key"),
        ({"update_columns": []}, "non-empty"),
        ({"update_columns": ["nope"]}, "unknown column"),
    ],
)
def test_options_are_validated(tmp_path, kw, match):
    args = {"mode": "upsert", "key": "id", **kw}
    with pytest.raises(ValueError, match=match):
        get("sqlite.write")(ROWS, db=str(tmp_path / "x.db"), table="t", **args)


def test_postgres_options_validated_before_connecting():
    pytest.importorskip("psycopg")
    with pytest.raises(ValueError, match="need mode: upsert"):
        get("postgres.write")(ROWS, dsn="postgresql://invalid", table="t", on_conflict="error")


def test_counts_in_events_and_run_summary(tmp_path):
    db = str(tmp_path / "x.db")
    write(db, ROWS)
    spec = parse(
        {
            "name": "upsert",
            "steps": [
                {"id": "src", "use": "json.read", "with": {"path": str(tmp_path / "in.json")}},
                {
                    "id": "save",
                    "use": "sqlite.write",
                    "input": "src",
                    "with": {"db": db, "table": "t", "mode": "upsert", "key": "id"},
                },
            ],
        }
    )
    (tmp_path / "in.json").write_text(json.dumps(ROWS))
    with Store(tmp_path / "state.db") as store:
        result = Runner(spec, store=store).run()
        events = store.events(result.run_id)
    assert result.status == "succeeded", result.error
    done = next(e for e in events if e["type"] == "step.succeeded" and e["step"] == "save")
    assert done["data"]["written"] == 3
    assert (done["data"]["inserted"], done["data"]["updated"], done["data"]["unchanged"]) == (0, 0, 3)
    assert result.summary()["writes"] == {"save": {"inserted": 0, "updated": 0, "unchanged": 3}}
