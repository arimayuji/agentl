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


def test_invoices_example_end_to_end(tmp_path, monkeypatch):
    """examples/invoices: paginated API + CSV + pandas + agent + Postgres upsert, offline."""
    import http.server
    import json as _json
    import pathlib
    import threading
    from urllib.parse import parse_qs, urlparse

    pytest.importorskip("pandas")
    import agentl
    from agentl import register_provider
    from agentl.llm import Result

    invoices = [
        {
            "number": f"INV-{i}",
            "tax_id": "12.345.678/0001-90" if i % 2 else "99.999.999/0001-99",
            "total": str(10 * i),
            "issued_at": "2026-10-13T10:00:00Z",
            "description": "SaaS seats",
        }
        for i in range(1, 6)
    ]

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urlparse(self.path)
            if u.path.endswith(".csv"):
                body, ctype = b"tax_id;name;segment\n12345678000190;Acme;tech\n", "text/csv"
            else:
                page = int(parse_qs(u.query)["page"][0])
                chunk = invoices[(page - 1) * 2 : page * 2]
                body = _json.dumps({"items": chunk, "next_page": page + 1 if page * 2 < 5 else None}).encode()
                ctype = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    class Fake:
        def __init__(self, spec):
            pass

        def structured(self, instruction, record, schema):
            return Result(
                schema.model_validate({"category": "software", "needs_review": not record["customer_known"]}),
                5,
                2,
            )

        def text(self, instruction, content):
            return ""

    register_provider("openai", Fake)  # stand-in for the real API in this test
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(DSN) as conn:
        conn.execute("DROP TABLE IF EXISTS invoices")
    env = {
        "VENDOR_API_URL": f"http://127.0.0.1:{srv.server_address[1]}",
        "VENDOR_API_TOKEN": "t",
        "SINCE": "2026-10-12",
        "DAY": "2026-10-13",
        "RAW_BUCKET": str(tmp_path),
        "DATABASE_URL": DSN,
        "SLACK_WEBHOOK": "http://127.0.0.1:9/none",
    }
    example = pathlib.Path(__file__).parents[1] / "examples" / "invoices" / "pipeline.yaml"
    try:
        result = agentl.run(example, vars=env, db=tmp_path / "s.db", raise_on_failure=True)
    finally:
        srv.shutdown()
        srv.server_close()
        from agentl import llm

        llm._PROVIDERS["openai"] = llm.OpenAIProvider
    assert result.summary()["records"]["invoices"] == 5
    rows = get("postgres.read")(
        None, dsn=DSN, query="SELECT number, name, category, needs_review FROM invoices ORDER BY number"
    )
    assert [r["number"] for r in rows] == ["INV-1", "INV-2", "INV-3", "INV-4", "INV-5"]
    assert rows[0]["name"] == "Acme" and rows[0]["needs_review"] is False
    assert rows[1]["name"] is None and rows[1]["needs_review"] is True
    assert (tmp_path / "invoices" / "2026-10-13" / "raw.json").exists()


def test_rejected_records_are_kept_and_retried_on_postgres():
    from agentl import connector, parse
    from agentl.runner import Runner, retry_rejected
    from agentl.store import open_store

    @connector("test.pg_quarantine_src")
    def src(input):
        return [{"n": "1"}, {"n": "x"}]

    pipe = f"pg_q_{uuid.uuid4().hex[:6]}"
    spec = parse(
        {
            "name": pipe,
            "steps": [
                {"id": "src", "use": "test.pg_quarantine_src"},
                {"id": "typed", "use": "cast", "input": "src", "with": {"fields": {"n": "int"}}},
            ],
        }
    )
    with open_store(DSN) as store:
        Runner(spec, store=store).run()
        (row,) = store.rejected_list(pipe)
        assert row["step"] == "typed" and row["record"]["n"] == "x"
        result = retry_rejected(spec, store)
        assert result is not None and result.status == "succeeded"
        assert store.rejected_get(row["id"])["retried_by"] == result.run_id
