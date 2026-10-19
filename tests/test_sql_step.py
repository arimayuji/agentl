"""`sql` step (#121): previous steps queried as tables in an in-process DuckDB."""

import csv
import datetime as dt
from decimal import Decimal

import pytest

pytest.importorskip("duckdb")
pa = pytest.importorskip("pyarrow")

from agentl import parse  # noqa: E402
from agentl.connectors import duck  # noqa: E402
from agentl.lint import check_text  # noqa: E402
from agentl.registry import connector  # noqa: E402
from agentl.runner import Runner  # noqa: E402
from agentl.store import Store  # noqa: E402

ORDERS = [
    {"order_id": 1, "customer_id": 10, "total": 50.0, "day": "2026-01-01"},
    {"order_id": 2, "customer_id": 10, "total": 70.0, "day": "2026-01-02"},
    {"order_id": 3, "customer_id": 20, "total": 30.0, "day": "2026-01-02"},
]
CUSTOMERS = [{"customer_id": 10, "region": "north"}, {"customer_id": 20, "region": "south"}]


@connector("test.sql_orders")
def _orders(input=None):
    return [dict(r) for r in ORDERS]


@connector("test.sql_customers")
def _customers(input=None):
    return [dict(r) for r in CUSTOMERS]


@connector("test.sql_empty")
def _empty(input=None):
    return []


def run(tmp_path, spec_dict):
    spec = parse(spec_dict)
    spec.base_dir = str(tmp_path)
    with Store(tmp_path / "s.db") as store:
        r = Runner(spec, store=store).run()
    return r


def test_single_input():
    out = duck.run("s", {"orders": ORDERS}, query="select order_id from orders where total > 40 order by 1")
    assert out == [{"order_id": 1}, {"order_id": 2}]


def test_join_two_inputs_through_runner(tmp_path):
    r = run(
        tmp_path,
        {
            "name": "p",
            "steps": [
                {"id": "orders", "use": "test.sql_orders"},
                {"id": "customers", "use": "test.sql_customers"},
                {
                    "id": "report",
                    "input": ["orders", "customers"],
                    "sql": "select c.region, sum(o.total) as total, count(*) as orders "
                    "from orders o join customers c using (customer_id) group by c.region order by 1",
                },
            ],
        },
    )
    assert r.status == "succeeded", r.error
    assert r.outputs["report"] == [
        {"region": "north", "total": 120.0, "orders": 2},
        {"region": "south", "total": 30.0, "orders": 1},
    ]


def test_window_function():
    out = duck.run(
        "s",
        {"orders": ORDERS},
        query="select order_id, sum(total) over (partition by customer_id order by order_id) as running "
        "from orders order by order_id",
    )
    assert [r["running"] for r in out] == [50.0, 120.0, 30.0]


def test_query_from_file(tmp_path):
    (tmp_path / "report.sql").write_text("select count(*) as n from orders", encoding="utf-8")
    r = run(
        tmp_path,
        {
            "name": "p",
            "steps": [
                {"id": "orders", "use": "test.sql_orders"},
                {"id": "n", "use": "sql", "input": "orders", "with": {"file": "report.sql"}},
            ],
        },
    )
    assert r.status == "succeeded", r.error
    assert r.outputs["n"] == [{"n": 3}]


def test_query_or_file_required():
    with pytest.raises(ValueError, match="exactly one of query"):
        duck.run("s", {}, query=None, file=None)


def test_arrow_input_registered_as_is():
    table = pa.table({"a": [1, 2, 3]})
    assert duck.run("s", {"t": table}, query="select sum(a) as s from t") == [{"s": 6}]


def test_arrow_plan_keeps_table_for_sql(tmp_path):
    src = tmp_path / "in.csv"
    with src.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "amount"])
        w.writerows([[1, 2], [2, 3]])
    spec = parse(
        {
            "name": "p",
            "arrow": True,
            "steps": [
                {"id": "rows", "use": "csv.read", "with": {"path": str(src)}},
                {"id": "total", "input": "rows", "sql": "select count(*) as n from rows"},
            ],
        }
    )
    with Store(tmp_path / "s.db") as store:
        runner = Runner(spec, store=store)
        r = runner.run()
    assert r.status == "succeeded", r.error
    assert "rows" in runner._arrow_out
    assert r.outputs["total"] == [{"n": 2}]


def test_params_are_bound_not_interpolated():
    rows = [{"name": "a"}, {"name": "b"}]
    q = "select name from t where name = $name"
    assert duck.run("s", {"t": rows}, query=q, params={"name": "a"}) == [{"name": "a"}]
    # A value that would be an injection if pasted into the SQL is only a string here.
    assert duck.run("s", {"t": rows}, query=q, params={"name": "a' or '1'='1"}) == []


def test_external_access_blocked_by_default(tmp_path):
    f = tmp_path / "secret.csv"
    f.write_text("x\n1\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"(?s)sql step 's'.*disabled"):
        duck.run("s", {}, query=f"select * from read_csv('{f}')")
    with pytest.raises(ValueError, match="sql step 's'"):
        duck.run("s", {}, query=f"copy (select 1) to '{tmp_path / 'out.csv'}'")
    with pytest.raises(ValueError, match="sql step 's'"):
        duck.run("s", {}, query="set enable_external_access = true")
    assert not (tmp_path / "out.csv").exists()


def test_allow_files(tmp_path):
    f = tmp_path / "data.csv"
    f.write_text("x\n1\n2\n", encoding="utf-8")
    out = duck.run("s", {}, query=f"select sum(x) as s from read_csv('{f}')", allow_files=True)
    assert out == [{"s": 3}]


def test_unknown_table_suggests_step_id():
    with pytest.raises(ValueError, match=r"sql step 'report'.*'ordrs'.*Did you mean 'orders'"):
        duck.run("report", {"orders": ORDERS}, query="select * from ordrs")


def test_syntax_error_has_step_id_and_line():
    with pytest.raises(ValueError, match=r"(?s)sql step 'r'.*LINE 2") as e:
        duck.run("r", {"t": ORDERS}, query="select order_id\nfrom t where totl > 1")
    assert "totl" in str(e.value)


def test_empty_inputs(tmp_path):
    assert duck.run("s", {"t": []}, query="select count(*) as n from t") == [{"n": 0}]
    assert duck.run("s", {"t": []}, query="select * from t") == []
    assert duck.run("s", {"t": []}, query="select id, sum(x) from t group by id") == []
    r = run(
        tmp_path,
        {
            "name": "p",
            "steps": [
                {"id": "none", "use": "test.sql_empty"},
                {"id": "q", "input": "none", "sql": "select region from none"},
            ],
        },
    )
    assert r.status == "succeeded", r.error
    assert r.outputs["q"] == []


def test_types_decimal_and_timestamp_with_zone():
    rows = [{"amount": Decimal("10.25"), "ts": dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc)}]
    out = duck.run("s", {"t": rows}, query="select amount * 2 as a, ts from t")
    assert out[0]["a"] == Decimal("20.50")
    assert out[0]["ts"] == dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc)


def test_chunked_pipeline_collects_at_sql(tmp_path):
    r = run(
        tmp_path,
        {
            "name": "p",
            "chunk_size": 1,
            "steps": [
                {"id": "orders", "use": "test.sql_orders"},
                {"id": "picked", "use": "select", "input": "orders", "with": {"fields": ["total"]}},
                {"id": "sum", "input": "picked", "sql": "select sum(total) as s from picked"},
            ],
        },
    )
    assert r.status == "succeeded", r.error
    assert r.outputs["sum"] == [{"s": 150.0}]


def test_connector_called_directly():
    from agentl.registry import get

    assert get("sql")([{"a": 1}], query="select a + 1 as b from input") == [{"b": 2}]


def test_sql_and_use_conflict():
    with pytest.raises(ValueError, match="sql is a step kind"):
        parse({"name": "p", "steps": [{"id": "x", "use": "select", "sql": "select 1"}]})


def test_lint_unknown_table():
    text = """
name: p
steps:
  - id: orders
    use: test.sql_orders
  - id: customers
    use: test.sql_customers
  - id: report
    input: orders
    sql: |
      with big as (select * from orders where total > 1)
      select * from big join customers using (customer_id)
"""
    report = check_text(text)
    codes = [f.code for f in report.errors]
    assert codes == ["AE009"]
    assert "customers" in report.errors[0].message


def test_lint_ok():
    text = """
name: p
steps:
  - id: orders
    use: test.sql_orders
  - id: report
    input: orders
    sql: select count(*) from orders
"""
    assert check_text(text).errors == []


def test_empty_input_keeps_known_columns_for_joins(tmp_path):
    """#288: a left join against an empty input returns every left row."""
    import sqlite3

    db = str(tmp_path / "prev.db")
    with sqlite3.connect(db) as conn:
        conn.execute("create table previous (k integer, v text)")
    query = "select r.order_id, p.v from rows r left join previous p on r.order_id = p.k order by 1"
    r = run(
        tmp_path,
        {
            "name": "p",
            "steps": [
                {"id": "rows", "use": "test.sql_orders"},
                {
                    "id": "previous",
                    "use": "sqlite.read",
                    "with": {"db": db, "query": "select * from previous"},
                },
                {
                    "id": "j",
                    "input": ["rows", "previous"],
                    "sql": query,
                },
            ],
        },
    )
    assert r.status == "succeeded", r.error
    assert r.outputs["j"] == [{"order_id": i, "v": None} for i in (1, 2, 3)]
    # Columns given directly (the runner passes a step's declared `schema:` or `output:`).
    query = "select t.order_id, e.v from t left join e on t.order_id = e.k"
    out = duck.run("s", {"t": ORDERS[:1], "e": []}, query=query, columns={"e": ["k", "v"]})
    assert out == [{"order_id": 1, "v": None}]


def test_empty_input_with_unknown_columns_fails_loudly_in_a_join():
    with pytest.raises(ValueError, match=r"sql step 's'.*'e' has no records and no known columns"):
        duck.run(
            "s", {"t": ORDERS, "e": []}, query="select t.order_id from t left join e on t.order_id = e.k"
        )


def test_empty_input_uses_the_declared_schema(tmp_path):
    r = run(
        tmp_path,
        {
            "name": "p",
            "steps": [
                {"id": "rows", "use": "test.sql_orders"},
                {"id": "none", "use": "test.sql_empty", "schema": {"k": "int", "v": "text"}},
                {
                    "id": "j",
                    "input": ["rows", "none"],
                    "sql": "select r.order_id, n.v from rows r left join none n on "
                    "r.order_id = n.k order by 1",
                },
            ],
        },
    )
    assert r.status == "succeeded", r.error
    assert [x["order_id"] for x in r.outputs["j"]] == [1, 2, 3]
