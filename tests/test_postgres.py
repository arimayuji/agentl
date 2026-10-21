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


def test_upsert_rerun_leaves_table_unchanged(table):
    write = get("postgres.write")
    rows = [{"id": 1, "v": "a", "meta": {"x": 1}}, {"id": 2, "v": "b", "meta": None}]
    first = write(rows, dsn=DSN, table=table, mode="upsert", key="id")[0]
    assert (first["inserted"], first["updated"], first["unchanged"]) == (2, 0, 0)
    before = read(table)
    again = write(rows, dsn=DSN, table=table, mode="upsert", key="id")[0]
    assert (again["written"], again["inserted"], again["updated"], again["unchanged"]) == (2, 0, 0, 2)
    assert read(table) == before


def test_upsert_counts_and_update_columns(table):
    write = get("postgres.write")
    write(
        [{"id": 1, "v": "a", "n": 1}, {"id": 2, "v": "b", "n": 2}],
        dsn=DSN,
        table=table,
        mode="upsert",
        key="id",
    )
    out = write(
        [{"id": 1, "v": "X", "n": 1}, {"id": 2, "v": "Y", "n": 20}, {"id": 3, "v": "c", "n": 3}],
        dsn=DSN,
        table=table,
        mode="upsert",
        key="id",
        update_columns=["n"],
    )[0]
    assert (out["inserted"], out["updated"], out["unchanged"]) == (1, 1, 1)
    assert [(r["id"], r["v"], r["n"]) for r in read(table)] == [(1, "a", 1), (2, "b", 20), (3, "c", 3)]


def test_upsert_do_nothing_and_error(table):
    write = get("postgres.write")
    write([{"id": 1, "v": "a"}], dsn=DSN, table=table, mode="upsert", key="id")
    out = write(
        [{"id": 1, "v": "X"}, {"id": 2, "v": "b"}],
        dsn=DSN,
        table=table,
        mode="upsert",
        key="id",
        on_conflict="do_nothing",
    )[0]
    assert (out["inserted"], out["updated"], out["unchanged"]) == (1, 0, 1)
    psycopg = pytest.importorskip("psycopg")
    with pytest.raises(psycopg.errors.UniqueViolation):
        write([{"id": 1, "v": "Z"}], dsn=DSN, table=table, mode="upsert", key="id", on_conflict="error")
    assert [(r["id"], r["v"]) for r in read(table)] == [(1, "a"), (2, "b")]


def test_upsert_options_are_validated():
    write = get("postgres.write")
    with pytest.raises(ValueError, match="need mode: upsert"):
        write([{"id": 1}], dsn=DSN, table="x", on_conflict="do_nothing")
    with pytest.raises(ValueError, match="on_conflict must be one of"):
        write([{"id": 1}], dsn=DSN, table="x", mode="upsert", key="id", on_conflict="merge")


def test_scd2_versions_rows(table):
    import datetime as dt

    write = get("postgres.write")

    def scd2(rows, as_of, **kw):
        return write(rows, dsn=DSN, table=table, mode="scd2", key="id", as_of=as_of, **kw)[0]

    def hist():
        q = f'SELECT * FROM "{table}" ORDER BY id, valid_from'
        return get("postgres.read")(None, dsn=DSN, query=q)

    def counts(out):
        return out["inserted"], out["versioned"], out["unchanged"]

    first = [{"id": 1, "city": "Lima", "meta": {"a": 1}}, {"id": 2, "city": "Oslo", "meta": None}]
    assert counts(scd2(first, "2026-01-01T21:00:00-03:00")) == (2, 0, 0)
    out = scd2(
        [{"id": 1, "city": "Lima", "meta": {"a": 1}}, {"id": 2, "city": "Rome", "meta": None}, {"id": 3}],
        "2026-02-01T00:00:00Z",
    )
    assert counts(out) == (1, 1, 1)
    got = hist()
    assert [(r["id"], r["city"], r["is_current"]) for r in got] == [
        (1, "Lima", True),
        (2, "Oslo", False),
        (2, "Rome", True),
        (3, None, True),
    ]
    utc = dt.timezone.utc
    assert got[0]["valid_from"] == dt.datetime(2026, 1, 2, tzinfo=utc)
    assert got[1]["valid_to"] == got[2]["valid_from"] == dt.datetime(2026, 2, 1, tzinfo=utc)
    before = hist()
    again = scd2(
        [{"id": 1, "city": "Lima", "meta": {"a": 1}}, {"id": 2, "city": "Rome", "meta": None}, {"id": 3}],
        "2026-03-01T00:00:00Z",
    )
    assert counts(again) == (0, 0, 3)
    assert hist() == before
    out = scd2(
        [{"id": 1, "city": "Lima"}, {"id": 1, "city": "Pisa"}], "2026-04-01T00:00:00Z", on_missing="close"
    )
    assert counts(out) == (0, 1, 0) and out["closed"] == 2
    assert [(r["id"], r["city"]) for r in hist() if r["is_current"]] == [(1, "Pisa")]
    with pytest.raises(ValueError, match="before the start"):
        scd2([{"id": 1, "city": "Bern"}], "2026-01-15T00:00:00Z")


def test_sql_write_scd2_on_postgres(table):
    pytest.importorskip("sqlalchemy")
    url = DSN.replace("postgresql://", "postgresql+psycopg://", 1)
    write = get("sql.write")
    kw = {"url": url, "table": table, "mode": "scd2", "key": "id"}
    write([{"id": 1, "v": "a"}], as_of="2026-01-01T00:00:00-03:00", **kw)
    out = write([{"id": 1, "v": "b"}, {"id": 2, "v": "c"}], as_of="2026-02-01T00:00:00Z", **kw)[0]
    assert (out["inserted"], out["versioned"], out["unchanged"]) == (1, 1, 0)
    again = write([{"id": 1, "v": "b"}, {"id": 2, "v": "c"}], as_of="2026-03-01T00:00:00Z", **kw)[0]
    assert again["unchanged"] == 2
    rows = sorted(read(table), key=lambda r: (r["id"], r["valid_from"]))
    assert [(r["id"], r["v"], r["is_current"]) for r in rows] == [
        (1, "a", False),
        (1, "b", True),
        (2, "c", True),
    ]
    assert rows[0]["valid_from"].isoformat() == "2026-01-01T03:00:00+00:00"


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


def test_leases_and_single_flight_across_workers():
    """Two connections act as two hosts: one lease holder, one computation (#204)."""
    import threading
    import time

    from agentl.store import PostgresStore

    a, b = PostgresStore(DSN), PostgresStore(DSN)
    try:
        lock = f"run:{uuid.uuid4().hex}"
        assert a.lock_acquire(lock, "host-a", 0.3)
        assert not b.lock_acquire(lock, "host-b", 0.3)
        time.sleep(0.4)  # host-a died without releasing
        assert b.lock_acquire(lock, "host-b", 5)
        b.lock_release(lock, "host-b")

        key, calls, out = f"sf:{uuid.uuid4().hex}", [], []

        def compute():
            calls.append(1)
            time.sleep(0.3)
            return [{"ok": True}]

        threads = [
            threading.Thread(target=lambda s=s: out.append(s.cache_compute(key, compute))) for s in (a, b)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(calls) == 1 and out == [[{"ok": True}]] * 2
        a.cache_clear(key)
    finally:
        a.close()
        b.close()


def test_upsert_rejects_null_keys(table):
    write = get("postgres.write")
    data = [{"id": 1, "v": "a"}, {"id": None, "v": "x"}, {"id": None, "v": "y"}]
    out = write(data, dsn=DSN, table=table, mode="upsert", key="id")
    assert out[0]["written"] == out[0]["inserted"] == 1
    assert [r["v"] for r in out.rejected] == ["x", "y"] and "is null" in out.rejected[0]["_error"]
    assert [r["v"] for r in read(table)] == ["a"]


def test_review_queue_on_postgres():
    import uuid

    from agentl.store import open_store

    pipeline = f"review-{uuid.uuid4().hex[:8]}"
    with open_store(DSN) as store:
        assert store.review_add("r1", pipeline, "s", "k1", "low", {"a": 1}, {"c": "x"})
        assert not store.review_add("r2", pipeline, "s", "k1", "low", {"a": 1}, {"c": "x"})
        [item] = store.review_list(pipeline)
        store.review_resolve(item["id"], "fixed", {"c": "y"})
        assert store.review_decisions(pipeline, "s", ["k1", "k2"]) == {
            "k1": {"status": "fixed", "value": {"c": "y"}}
        }
        assert store.cache_get("k1") is None  # the queue keeps it; the agent cache has its own keys
        assert store.review_counts(pipeline) == [
            {"pipeline": pipeline, "step": "s", "status": "fixed", "count": 1}
        ]
        store.cache_clear("k1")


def test_postgres_errors_are_classified_as_conflicts(table):
    import threading

    psycopg = pytest.importorskip("psycopg")
    from agentl.runner import sql_conflict

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE TABLE "{table}" (id int primary key, v int)')
        conn.execute(f'INSERT INTO "{table}" VALUES (1, 0), (2, 0)')
        with pytest.raises(psycopg.Error) as dup:
            conn.execute(f'INSERT INTO "{table}" VALUES (1, 0)')
    assert sql_conflict(dup.value) == "unique_violation"

    a, b = psycopg.connect(DSN), psycopg.connect(DSN)
    errors: list[BaseException] = []
    try:
        for c in (a, b):
            c.execute("SET deadlock_timeout = '50ms'")
        a.execute(f'UPDATE "{table}" SET v = 1 WHERE id = 1')
        b.execute(f'UPDATE "{table}" SET v = 1 WHERE id = 2')

        def cross(conn, row):
            try:
                conn.execute(f'UPDATE "{table}" SET v = 2 WHERE id = {row}')
            except psycopg.Error as e:
                errors.append(e)
                conn.rollback()

        t = threading.Thread(target=cross, args=(a, 2))
        t.start()
        cross(b, 1)
        t.join(10)
    finally:
        a.close()
        b.close()
    assert len(errors) == 1 and sql_conflict(errors[0]) == "deadlock"


def two_rows(input=None):
    return [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]


def test_postgres_write_retries_a_lock_conflict(table, tmp_path, monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    from agentl import parse
    from agentl import runner as runner_mod
    from agentl.runner import Runner
    from agentl.store import Store

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE TABLE "{table}" (id int primary key, v text)')
    locker = psycopg.connect(DSN)
    locker.execute(f'LOCK TABLE "{table}" IN ACCESS EXCLUSIVE MODE')
    waits: list[float] = []

    def release(seconds):
        waits.append(seconds)
        locker.rollback()

    monkeypatch.setattr(runner_mod, "_sleep", release)
    monkeypatch.setattr(runner_mod, "_jitter", lambda: 1.0)
    sep = "&" if "?" in DSN else "?"
    spec = parse(
        {
            "name": "pg_lock",
            "steps": [
                {"id": "rows", "fn": "tests.test_postgres:two_rows"},
                {
                    "id": "save",
                    "use": "postgres.write",
                    "input": "rows",
                    "with": {
                        "dsn": DSN + sep + "options=-c%20lock_timeout%3D100ms",
                        "table": table,
                        "mode": "upsert",
                        "key": "id",
                    },
                },
            ],
        }
    )
    try:
        with Store(tmp_path / "a.db") as store:
            result = Runner(spec, store=store).run()
            events = store.events(result.run_id)
    finally:
        locker.close()
    assert result.status == "succeeded", result.error
    assert waits == [0.2]
    assert any(e["type"] == "step.retry" for e in events)
    assert [r["v"] for r in read(table)] == ["a", "b"]
