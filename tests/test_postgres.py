import os
import uuid

import pytest

from agentl.registry import get

DSN = os.environ.get("AGENTL_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set AGENTL_TEST_PG_DSN to run Postgres tests")


@pytest.fixture
def table():
    name = f"t_{uuid.uuid4().hex[:8]}"
    yield name
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(DSN) as conn:
        conn.execute(f'DROP TABLE IF EXISTS "{name}"')


def read(table):
    return get("postgres.read")(None, dsn=DSN, query=f'SELECT * FROM "{table}" ORDER BY id')


def test_append_creates_typed_table(table):
    rows = [{"id": 1, "name": "Ana", "score": 9.5, "ok": True, "meta": {"a": 1}}, {"id": 2, "name": None}]
    out = get("postgres.write")(rows, dsn=DSN, table=table)
    assert out == [{"table": table, "written": 2, "mode": "append"}]
    got = read(table)
    assert got[0] == {"id": 1, "name": "Ana", "score": 9.5, "ok": True, "meta": {"a": 1}}
    assert got[1]["name"] is None


def test_upsert_updates_on_key(table):
    write = get("postgres.write")
    write([{"id": 1, "v": "a"}, {"id": 2, "v": "b"}], dsn=DSN, table=table, mode="upsert", key="id")
    write([{"id": 2, "v": "B"}, {"id": 3, "v": "c"}], dsn=DSN, table=table, mode="upsert", key="id")
    assert [(r["id"], r["v"]) for r in read(table)] == [(1, "a"), (2, "B"), (3, "c")]


def test_replace_and_params(table):
    write = get("postgres.write")
    write([{"id": 1}], dsn=DSN, table=table)
    write([{"id": 5}, {"id": 6}], dsn=DSN, table=table, mode="replace", batch_size=1)
    got = get("postgres.read")(None, dsn=DSN, query=f'SELECT id FROM "{table}" WHERE id > %s', params=[5])
    assert got == [{"id": 6}]


def test_identifiers_are_quoted(table):
    rows = [{"id": 1, 'we"ird; DROP': "x"}]
    get("postgres.write")(rows, dsn=DSN, table=table)
    assert read(table)[0]['we"ird; DROP'] == "x"


def test_bad_args():
    with pytest.raises(ValueError, match="needs 'key'"):
        get("postgres.write")([{"id": 1}], dsn=DSN, table="x", mode="upsert")
    with pytest.raises(ValueError, match="mode"):
        get("postgres.write")([{"id": 1}], dsn=DSN, table="x", mode="merge")


def test_sales_example_is_idempotent(tmp_path, monkeypatch):
    """examples/sales: CSV over HTTP -> pandas step -> Postgres upsert, run twice."""
    import http.server
    import pathlib
    import threading
    from functools import partial

    pytest.importorskip("pandas")
    from agentl.cli import main

    example = pathlib.Path(__file__).parents[1] / "examples" / "sales"
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(example))
    handler.log_message = lambda *a: None  # type: ignore[attr-defined]
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(DSN) as conn:
        conn.execute("DROP TABLE IF EXISTS sales")
    monkeypatch.setenv("SALES_URL", f"http://127.0.0.1:{httpd.server_address[1]}/sales.csv")
    monkeypatch.setenv("DATABASE_URL", DSN)
    try:
        for _ in range(2):
            assert main(["run", str(example / "pipeline.yaml"), "--db", str(tmp_path / "s.db")]) == 0
    finally:
        httpd.shutdown()
        httpd.server_close()
    rows = get("postgres.read")(None, dsn=DSN, query="SELECT * FROM sales ORDER BY order_id")
    assert rows == [
        {"order_id": 1, "country": "BR", "total": 21.0},
        {"order_id": 2, "country": "US", "total": 99.0},
    ]
