import sqlite3

import pytest

from agentl.connectors.sql import sqlite_write_chunks
from agentl.registry import get

JAN = "2026-01-01T00:00:00Z"
FEB = "2026-02-01T00:00:00Z"
MAR = "2026-03-01T00:00:00Z"


def write(db, rows, **kw):
    kw.setdefault("key", "id")
    return get("sqlite.write")(rows, db=str(db), table="dim", mode="scd2", **kw)[0]


def table(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    q = "SELECT * FROM dim ORDER BY id, valid_from"
    return [dict(r) for r in conn.execute(q)]


def counts(out):
    return out["inserted"], out["versioned"], out["unchanged"]


def test_new_changed_unchanged_and_rerun(tmp_path):
    db = tmp_path / "w.db"
    rows = [{"id": 1, "city": "Lima"}, {"id": 2, "city": "Oslo"}]
    assert counts(write(db, rows, as_of=JAN)) == (2, 0, 0)
    out = write(
        db, [{"id": 1, "city": "Lima"}, {"id": 2, "city": "Rome"}, {"id": 3, "city": "Kyiv"}], as_of=FEB
    )
    assert counts(out) == (1, 1, 1) and out["written"] == 3
    got = table(db)
    assert [(r["id"], r["city"], r["is_current"]) for r in got] == [
        (1, "Lima", 1),
        (2, "Oslo", 0),
        (2, "Rome", 1),
        (3, "Kyiv", 1),
    ]
    old = got[1]
    assert old["valid_from"].startswith("2026-01-01T00:00:00") and old["valid_to"] == got[2]["valid_from"]
    assert got[2]["valid_to"] is None
    before = table(db)
    again = write(
        db, [{"id": 1, "city": "Lima"}, {"id": 2, "city": "Rome"}, {"id": 3, "city": "Kyiv"}], as_of=MAR
    )
    assert counts(again) == (0, 0, 3)
    assert table(db) == before


def test_missing_rows_keep_or_close(tmp_path):
    db = tmp_path / "w.db"
    write(db, [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}], as_of=JAN)
    out = write(db, [{"id": 1, "v": "a"}], as_of=FEB)
    assert "closed" not in out and sum(r["is_current"] for r in table(db)) == 2
    out = write(db, [{"id": 1, "v": "a"}], as_of=FEB, on_missing="close")
    assert out["closed"] == 1
    gone = next(r for r in table(db) if r["id"] == 2)
    assert gone["is_current"] == 0 and gone["valid_to"].startswith("2026-02-01")
    # a key that comes back opens a new version
    out = write(db, [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}], as_of=MAR, on_missing="close")
    assert counts(out) == (1, 0, 1) and out["closed"] == 0
    assert [r["is_current"] for r in table(db) if r["id"] == 2] == [0, 1]


def test_track_limits_what_opens_a_version(tmp_path):
    db = tmp_path / "w.db"
    write(db, [{"id": 1, "price": 10, "note": "x"}], as_of=JAN, track="price")
    assert counts(write(db, [{"id": 1, "price": 10, "note": "y"}], as_of=FEB, track="price")) == (0, 0, 1)
    assert counts(write(db, [{"id": 1, "price": 12, "note": "y"}], as_of=MAR, track=["price"])) == (0, 1, 0)
    assert [(r["price"], r["note"]) for r in table(db)] == [(10, "x"), (12, "y")]


def test_time_zones_are_stored_as_utc(tmp_path):
    db = tmp_path / "w.db"
    out = write(db, [{"id": 1, "v": "a"}], as_of="2026-01-01T21:00:00-03:00")
    assert out["as_of"] == "2026-01-02T00:00:00.000000+00:00"
    write(db, [{"id": 1, "v": "b"}], as_of="2026-01-02T09:00:00+09:00")  # 00:00 UTC, same instant
    first, second = table(db)
    assert first["valid_to"] == second["valid_from"] == "2026-01-02T00:00:00.000000+00:00"
    with pytest.raises(ValueError, match="time zone"):
        write(db, [{"id": 1, "v": "c"}], as_of="2026-03-01T00:00:00")
    with pytest.raises(ValueError, match="before the start"):
        write(db, [{"id": 1, "v": "c"}], as_of="2025-12-01T00:00:00Z")


def test_default_as_of_is_now_utc(tmp_path):
    db = tmp_path / "w.db"
    out = write(db, [{"id": 1}])
    assert out["as_of"].endswith("+00:00") and table(db)[0]["valid_from"] == out["as_of"]


def test_duplicates_last_wins_and_chunks(tmp_path):
    db = tmp_path / "w.db"
    chunks = [[{"id": 1, "v": "a"}], [{"id": 1, "v": "b"}, {"id": 2, "v": "c", "extra": 1}]]
    out = sqlite_write_chunks(chunks, db=str(db), table="dim", mode="scd2", key="id", as_of=JAN)[0]
    assert counts(out) == (2, 0, 0)
    assert [(r["id"], r["v"], r["extra"]) for r in table(db)] == [(1, "b", None), (2, "c", 1)]


def test_bad_options(tmp_path):
    db = tmp_path / "w.db"
    with pytest.raises(ValueError, match="needs `key`"):
        write(db, [{"id": 1}], key=None)
    with pytest.raises(ValueError, match="need mode: scd2"):
        get("sqlite.write")([{"id": 1}], db=str(db), table="dim", track="v")
    with pytest.raises(ValueError, match="on_missing"):
        write(db, [{"id": 1}], on_missing="drop")
    with pytest.raises(ValueError, match="manages"):
        write(db, [{"id": 1, "is_current": 1}])
    with pytest.raises(ValueError, match="unknown"):
        write(db, [{"id": 1}], track="nope")
    with pytest.raises(ValueError, match="every key"):
        write(db, [{"id": None, "v": 1}])
    assert write(db, [], on_missing="close")["written"] == 0


def test_sql_write_scd2(tmp_path):
    pytest.importorskip("sqlalchemy")
    url = f"sqlite:///{tmp_path / 's.db'}"

    def go(rows, as_of, **kw):
        return get("sql.write")(rows, url=url, table="dim", mode="scd2", key="id", as_of=as_of, **kw)[0]

    assert counts(go([{"id": 1, "v": "a"}, {"id": 2, "v": "b"}], JAN)) == (2, 0, 0)
    out = go([{"id": 1, "v": "a"}, {"id": 2, "v": "c"}, {"id": 3, "v": "d"}], FEB)
    assert counts(out) == (1, 1, 1)
    assert counts(go([{"id": 1, "v": "a"}, {"id": 2, "v": "c"}, {"id": 3, "v": "d"}], MAR)) == (0, 0, 3)
    out = go([{"id": 1, "v": "a"}], MAR, on_missing="close")
    assert out["closed"] == 2
    rows = get("sql.read")(None, url=url, query="SELECT id, v, is_current FROM dim ORDER BY id, valid_from")
    assert [(r["id"], r["v"], r["is_current"]) for r in rows] == [
        (1, "a", 1),
        (2, "b", 0),
        (2, "c", 0),
        (3, "d", 0),
    ]
    with pytest.raises(ValueError, match="before the start"):
        go([{"id": 1, "v": "z"}], "2025-12-01T00:00:00Z")
