"""Arrow fast path (#60): same results with and without it."""

import csv
import os

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
pytest.importorskip("pandas")

from agentl import parse  # noqa: E402
from agentl.runner import Runner  # noqa: E402
from agentl.store import Store  # noqa: E402
from tests.arrow_steps import steps  # noqa: E402


def make_csv(path, n=50, quirky=False):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "kind", "amount", "note"])
        for i in range(n):
            note = 'line\nbreak, "quoted"' if quirky and i == 3 else ("" if i % 7 == 0 else f"n{i}")
            w.writerow([i, "ab"[i % 2], f"{i * 1.37:.3f}", note])
    return str(path)


def run(tmp_path, spec_dict, arrow):
    spec = parse({**spec_dict, "arrow": arrow})
    with Store(tmp_path / f"state-{arrow}.db") as store:
        runner = Runner(spec, store=store)
        r = runner.run()
        assert r.status == "succeeded", r.error
        return r, runner


def csv_pandas_parquet(src, out):
    return {
        "name": "a",
        "steps": [
            {"id": "read", "use": "csv.read", "with": {"path": src}},
            {"id": "clean", "fn": "tests.arrow_steps.steps:clean", "input": "read"},
            {"id": "save", "use": "parquet.write", "input": "clean", "with": {"path": out}},
        ],
    }


@pytest.fixture(autouse=True)
def reset():
    steps.CALLS.clear()


def test_csv_pandas_parquet_matches_the_dict_path(tmp_path):
    src = make_csv(tmp_path / "in.csv", quirky=True)
    slow, _ = run(tmp_path, csv_pandas_parquet(src, str(tmp_path / "slow.parquet")), False)
    fast, runner = run(tmp_path, csv_pandas_parquet(src, str(tmp_path / "fast.parquet")), True)
    assert runner._arrow_out == {"read", "clean"}
    assert (
        pq.read_table(tmp_path / "fast.parquet").to_pylist()
        == pq.read_table(tmp_path / "slow.parquet").to_pylist()
    )
    assert pq.read_table(tmp_path / "fast.parquet").schema.equals(
        pq.read_table(tmp_path / "slow.parquet").schema
    )
    # outputs read the same through RunResult, converted on access
    assert fast.outputs["read"] == slow.outputs["read"]
    assert fast.outputs["clean"] == slow.outputs["clean"]
    assert fast.summary()["records"] == slow.summary()["records"]


def test_a_consumer_that_needs_records_turns_the_fast_path_off(tmp_path):
    src = make_csv(tmp_path / "in.csv")
    spec = {
        "name": "b",
        "steps": [
            {"id": "read", "use": "csv.read", "with": {"path": src}},
            {"id": "clean", "fn": "tests.arrow_steps.steps:clean", "input": "read"},
            {"id": "other", "fn": "tests.arrow_steps.steps:plain", "input": "read"},
        ],
    }
    r, runner = run(tmp_path, spec, True)
    assert runner._arrow_out == set()  # `read` feeds a plain fn too
    assert r.outputs["other"][0]["seen"] is True


def test_auto_mode_uses_arrow_when_installed(tmp_path):
    src = make_csv(tmp_path / "in.csv")
    _, runner = run(tmp_path, csv_pandas_parquet(src, str(tmp_path / "o.parquet")), None)
    assert runner._arrow_out == {"read", "clean"}
    assert steps.CALLS == ["DataFrame"]


def test_csv_quirks_fall_back_to_the_normal_reader(tmp_path):
    from agentl.connectors.files import csv_read, csv_read_arrow

    ragged = tmp_path / "ragged.csv"
    ragged.write_text("a,b\n1,2\n3\n4,5,6\n")
    dup = tmp_path / "dup.csv"
    dup.write_text("a,a\n1,2\n")
    bom = tmp_path / "bom.csv"
    bom.write_text("﻿a,b\n1,2\n", encoding="utf-8")
    for p in (ragged, dup, bom):
        got = csv_read_arrow(path=str(p))
        assert isinstance(got, list) and got == csv_read(path=str(p)), p.name
    ok = csv_read_arrow(path=make_csv(tmp_path / "ok.csv", quirky=True))
    assert ok.to_pylist() == csv_read(path=str(tmp_path / "ok.csv"))


def test_parquet_round_trip_both_paths(tmp_path):
    from agentl.connectors.files import parquet_read, parquet_read_arrow, parquet_write

    rows = [{"id": i, "v": f"x{i}", "f": i / 3, "none": None} for i in range(10)]
    parquet_write(rows, str(tmp_path / "a.parquet"))
    assert parquet_read(path=str(tmp_path / "a.parquet")) == rows
    assert parquet_read_arrow(path=str(tmp_path / "a.parquet")).to_pylist() == rows


PG = os.environ.get("AGENTL_TEST_PG_DSN")


@pytest.mark.skipif(not PG, reason="set AGENTL_TEST_PG_DSN to run Postgres tests")
def test_parquet_to_postgres_copy_matches_insert(tmp_path):
    import datetime as dt

    import psycopg

    from agentl.connectors.files import parquet_write

    rows = [
        {
            "id": i,
            "name": f"n{i}",
            "amount": i * 1.5,
            "ok": i % 2 == 0,
            "day": dt.date(2026, 1, 1 + i % 28),
            "note": None,
        }
        for i in range(200)
    ]
    parquet_write(rows, str(tmp_path / "in.parquet"))

    def spec(table):
        return {
            "name": "pg",
            "steps": [
                {"id": "read", "use": "parquet.read", "with": {"path": str(tmp_path / "in.parquet")}},
                {
                    "id": "save",
                    "use": "postgres.write",
                    "input": "read",
                    "with": {"dsn": PG, "table": table, "mode": "replace"},
                },
            ],
        }

    run(tmp_path, spec("agentl_arrow_slow"), False)
    _, runner = run(tmp_path, spec("agentl_arrow_fast"), True)
    assert runner._arrow_out == {"read"}
    with psycopg.connect(PG) as conn:

        def dump(t):
            cols = conn.execute(
                "select column_name, data_type from information_schema.columns"
                " where table_name = %s order by ordinal_position",
                [t],
            ).fetchall()
            return cols, conn.execute(f"select * from {t} order by id").fetchall()

        assert dump("agentl_arrow_fast") == dump("agentl_arrow_slow")


def test_upsert_keeps_records(tmp_path):
    src = tmp_path / "in.parquet"
    pq.write_table(pa.Table.from_pylist([{"id": 1}]), src)
    spec = parse(
        {
            "name": "u",
            "steps": [
                {"id": "read", "use": "parquet.read", "with": {"path": str(src)}},
                {
                    "id": "save",
                    "use": "postgres.write",
                    "input": "read",
                    "with": {"dsn": "x", "table": "t", "mode": "upsert", "key": "id"},
                },
            ],
        }
    )
    assert Runner(spec, store=Store(tmp_path / "s.db"))._arrow_plan({}) == set()


def test_arrow_true_without_pyarrow_is_a_clear_error(tmp_path, monkeypatch):
    from agentl import registry

    monkeypatch.setattr(registry, "arrow_available", lambda: False)
    spec = parse({"name": "x", "arrow": True, "steps": [{"id": "s", "use": "select"}]})
    with Store(tmp_path / "s.db") as store:
        r = Runner(spec, store=store).run()
    assert r.status == "failed" and "agentl-etl[arrow]" in r.error


def test_outputs_never_leak_a_table(tmp_path):
    import copy
    import json
    import pickle

    src = make_csv(tmp_path / "in.csv")
    r, runner = run(tmp_path, csv_pandas_parquet(src, str(tmp_path / "o.parquet")), True)
    assert runner._arrow_out == {"read", "clean"}
    for view in (
        dict(r.outputs),
        {**r.outputs},
        copy.deepcopy(r.outputs),
        pickle.loads(pickle.dumps(r.outputs)),
        dict(r.outputs.items()),
    ):
        assert all(isinstance(v, list) for v in dict.values(view))
    assert "pyarrow" not in repr(r.outputs) and "pyarrow" not in str(r.outputs)
    assert json.loads(json.dumps(r.outputs))["read"][0]["id"] == "0"
    assert all(isinstance(v, list) for v in dict.values(r.outputs))  # converted in place by now


def test_run_result_copies_and_pickles_without_arrow(tmp_path):
    import copy
    import pickle

    src = make_csv(tmp_path / "in.csv")
    spec = parse(
        {"name": "p", "arrow": False, "steps": [{"id": "s", "use": "csv.read", "with": {"path": src}}]}
    )
    with Store(tmp_path / "s.db") as store:
        r = Runner(spec, store=store).run()
    assert r.status == "succeeded" and len(r.outputs["s"]) == 50
    assert copy.deepcopy(r).outputs == r.outputs
    assert pickle.loads(pickle.dumps(r)).outputs == r.outputs
