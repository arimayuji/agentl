import os
import uuid

import pytest

from agentl.registry import get

pytest.importorskip("sqlalchemy")
PG = os.environ.get("AGENTL_TEST_PG_DSN")


@pytest.fixture(params=["sqlite", "postgres"])
def url(request, tmp_path):
    if request.param == "postgres":
        if not PG:
            pytest.skip("set AGENTL_TEST_PG_DSN to run Postgres tests")
        pytest.importorskip("psycopg")
        return PG.replace("postgresql://", "postgresql+psycopg://", 1)
    return f"sqlite:///{tmp_path / 'db.sqlite'}"


@pytest.fixture
def table():
    return f"t_{uuid.uuid4().hex[:8]}"


def read(url, table):
    return get("sql.read")(None, url=url, query=f'SELECT * FROM "{table}" ORDER BY id')


def test_append_replace_upsert(url, table):
    write = get("sql.write")
    write([{"id": 1, "v": "a", "n": 1.5, "ok": True}], url=url, table=table)
    write([{"id": 2, "v": "b", "n": 2.0, "ok": False}], url=url, table=table)  # append to existing
    assert [r["v"] for r in read(url, table)] == ["a", "b"]

    write([{"id": 2, "v": "B"}, {"id": 3, "v": "c"}], url=url, table=table, mode="upsert", key="id")
    assert [(r["id"], r["v"]) for r in read(url, table)] == [(1, "a"), (2, "B"), (3, "c")]

    write([{"id": 9, "v": "z"}], url=url, table=table, mode="replace", batch_size=1)
    assert [(r["id"], r["v"]) for r in read(url, table)] == [(9, "z")]

    got = get("sql.read")(None, url=url, query=f'SELECT v FROM "{table}" WHERE id = :id', params={"id": 9})
    assert got == [{"v": "z"}]


def test_portable_upsert_fallback(tmp_path, table, monkeypatch):
    """Databases without a native upsert get delete-then-insert in one transaction."""
    monkeypatch.setattr("agentl.connectors.db._upsert_stmt", lambda *a: None)
    url = f"sqlite:///{tmp_path / 'f.sqlite'}"
    write = get("sql.write")
    write([{"id": 1, "v": "a"}, {"id": 2, "v": "b"}], url=url, table=table, mode="upsert", key="id")
    write([{"id": 2, "v": "B"}], url=url, table=table, mode="upsert", key="id")
    assert [(r["id"], r["v"]) for r in read(url, table)] == [(1, "a"), (2, "B")]


def test_bad_args(tmp_path):
    url = f"sqlite:///{tmp_path / 'x.sqlite'}"
    with pytest.raises(ValueError, match="needs 'key'"):
        get("sql.write")([{"id": 1}], url=url, table="x", mode="upsert")
    with pytest.raises(ValueError, match="does not exist"):
        get("sql.write")([{"id": 1}], url=url, table="x", create=False)


def test_upsert_rejects_null_keys(url, table):
    out = get("sql.write")(
        [{"id": 1, "v": "a"}, {"id": None, "v": "x"}], url=url, table=table, mode="upsert", key="id"
    )
    assert out[0]["written"] == 1 and [r["v"] for r in out.rejected] == ["x"]
    assert [(r["id"], r["v"]) for r in read(url, table)] == [(1, "a")]
