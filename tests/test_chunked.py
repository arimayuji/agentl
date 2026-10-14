"""Chunked execution (#58): linear chains stream `chunk_size` records at a time."""

import csv
import os
import sqlite3

import pytest

from agentl import parse
from agentl.llm import Result, register_provider
from agentl.registry import connector, stream_reader, stream_writer
from agentl.runner import Runner
from agentl.store import Store

LOG: list[str] = []


@stream_reader("test.count")
def count_chunks(input=None, chunk_size=1, n=10):
    for start in range(0, n, chunk_size):
        LOG.append(f"read {start}")
        yield [{"i": i, "even": i % 2 == 0} for i in range(start, min(n, start + chunk_size))]


@connector("test.count")
def count_all(input=None, n=10):
    return [r for chunk in count_chunks(chunk_size=n or 1, n=n) for r in chunk]


@stream_writer("test.sink")
def sink_chunks(chunks):
    total = 0
    for rows in chunks:
        LOG.append(f"write {len(rows)}")
        total += len(rows)
    return [{"written": total}]


@connector("test.sink")
def sink(input):
    return sink_chunks([input])


@pytest.fixture(autouse=True)
def reset():
    LOG.clear()


def make_csv(path, n=10, extra_at=None):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "kind"])
        for i in range(n):
            w.writerow([i, "a" if i % 2 else "b"])
    return str(path)


def spec(steps, chunk_size=None, **kw):
    return parse({"name": "chunks", "chunk_size": chunk_size, "steps": steps, **kw})


def run(tmp_path, s, **kw):
    with Store(tmp_path / "state.db") as store:
        r = Runner(s, store=store, **kw).run()
        return r, store.events(r.run_id)


def csv_to_sqlite(src, db, chunk_size):
    return spec(
        [
            {"id": "read", "use": "csv.read", "with": {"path": src}},
            {"id": "keep", "use": "filter", "input": "read", "with": {"where": {"kind": "a"}}},
            {"id": "slim", "use": "select", "input": "keep", "with": {"fields": ["id"]}},
            {"id": "save", "use": "sqlite.write", "input": "slim", "with": {"db": db, "table": "t"}},
        ],
        chunk_size,
    )


def test_chunked_chain_matches_the_normal_run(tmp_path):
    src = make_csv(tmp_path / "in.csv", n=25)
    plain, _ = run(tmp_path / "p", csv_to_sqlite(src, str(tmp_path / "plain.db"), None))
    chunked, events = run(tmp_path / "c", csv_to_sqlite(src, str(tmp_path / "chunked.db"), 4))
    assert plain.status == chunked.status == "succeeded", chunked.error

    def rows(db):
        with sqlite3.connect(db) as conn:
            return conn.execute("select id from t order by rowid").fetchall()

    assert rows(tmp_path / "plain.db") == rows(tmp_path / "chunked.db")
    assert chunked.outputs == {"save": [{"table": "t", "written": 12}]}
    done = {e["step"]: e["data"] for e in events if e["type"] == "step.succeeded"}
    assert {k: v["records"] for k, v in done.items()} == {"read": 25, "keep": 12, "slim": 12, "save": 1}
    assert all(v["chunked"] for v in done.values())


def test_chunks_flow_through_one_at_a_time(tmp_path):
    s = spec(
        [
            {"id": "src", "use": "test.count", "with": {"n": 6}},
            {"id": "out", "use": "test.sink", "input": "src"},
        ],
        chunk_size=2,
    )
    r, _ = run(tmp_path, s)
    assert r.status == "succeeded", r.error
    assert LOG == ["read 0", "write 2", "read 2", "write 2", "read 4", "write 2"]


def test_without_chunk_size_nothing_streams(tmp_path):
    s = spec(
        [
            {"id": "src", "use": "test.count", "with": {"n": 6}},
            {"id": "out", "use": "test.sink", "input": "src"},
        ]
    )
    r, _ = run(tmp_path, s)
    assert r.outputs["src"] and LOG == ["read 0", "write 6"]


def test_boundaries_collect_records(tmp_path):
    s = spec(
        [
            {"id": "src", "use": "test.count", "with": {"n": 6}},
            {"id": "odd", "use": "filter", "input": "src", "with": {"where": {"even": False}}},
            {"id": "uniq", "use": "dedupe", "input": "odd", "with": {"on": "i"}},
            {"id": "out", "use": "test.sink", "input": "uniq"},
        ],
        chunk_size=2,
    )
    r, _ = run(tmp_path, s)
    assert r.status == "succeeded", r.error
    assert [x["i"] for x in r.outputs["odd"]] == [1, 3, 5]  # chain src -> odd ends at dedupe
    assert "src" not in r.outputs
    assert LOG[-1] == "write 3"  # dedupe and the sink ran normally on the collected records


def test_fan_out_is_not_streamed(tmp_path):
    s = spec(
        [
            {"id": "src", "use": "test.count", "with": {"n": 4}},
            {"id": "a", "use": "select", "input": "src"},
            {"id": "b", "use": "select", "input": "src"},
        ],
        chunk_size=2,
    )
    r, _ = run(tmp_path, s)
    assert len(r.outputs["src"]) == 4 and len(r.outputs["a"]) == len(r.outputs["b"]) == 4


class Tagger:
    def __init__(self, spec):
        pass

    def structured(self, instruction, record, schema):
        if record["i"] == 3:
            raise ValueError("bad record")
        return Result(schema.model_validate({"tag": f"t{record['i']}"}), 10, 1)

    def text(self, instruction, content):
        return ""


register_provider("tagger", Tagger)


def test_agent_steps_run_per_chunk_with_cache_spend_and_rejects(tmp_path):
    s = spec(
        [
            {"id": "src", "use": "test.count", "with": {"n": 6}},
            {"id": "tag", "agent": "Tag.", "input": "src", "output": {"tag": "str"}},
            {"id": "out", "use": "test.sink", "input": "tag"},
        ],
        chunk_size=2,
        llm={"default": {"provider": "tagger", "model": "m", "price": {"input": 1, "output": 1}}},
    )
    with Store(tmp_path / "state.db") as store:
        r = Runner(s, store=store).run()
        again = Runner(s, store=store).run()
    assert r.status == "succeeded", r.error
    assert r.outputs["out"] == [{"written": 5}]
    assert [x["i"] for x in r.rejected["tag"]] == [3]
    assert r.spend["tag"]["llm_calls"] == 7  # 5 answers + 2 tries for the bad record
    assert again.llm_calls == 2  # cached records are not asked again; the bad one is retried


def test_failure_names_the_step_that_broke(tmp_path):
    src = make_csv(tmp_path / "in.csv")
    s = spec(
        [
            {"id": "read", "use": "csv.read", "with": {"path": src}},
            {"id": "save", "use": "csv.write", "input": "read", "with": {"path": str(tmp_path / "o.csv")}},
            {"id": "x", "use": "csv.read", "with": {"path": str(tmp_path / "missing.csv")}},
            {"id": "y", "use": "select", "input": "x"},
        ],
        chunk_size=3,
    )
    r, events = run(tmp_path, s)
    assert r.status == "failed" and "missing.csv" in r.error
    failed = {e["step"]: e["data"]["error"] for e in events if e["type"] == "step.failed"}
    assert failed["x"].startswith("FileNotFoundError")
    assert failed["y"] == "stopped: step 'x' failed"


def test_csv_write_refuses_a_column_that_appears_later(tmp_path):
    from agentl.connectors.files import csv_write_chunks

    with pytest.raises(ValueError, match="not in the first chunk"):
        csv_write_chunks([[{"a": 1}], [{"a": 2, "b": 3}]], str(tmp_path / "o.csv"))


def test_sqlite_writer_adds_columns_that_appear_later(tmp_path):
    from agentl.connectors.sql import sqlite_write_chunks

    db = str(tmp_path / "x.db")
    out = sqlite_write_chunks([[{"a": 1}], [{"a": 2, "b": "x"}]], db=db, table="t")
    assert out == [{"table": "t", "written": 2}]
    with sqlite3.connect(db) as conn:
        assert conn.execute("select a, b from t order by a").fetchall() == [(1, None), (2, "x")]


def test_jsonl_streams_and_json_collects(tmp_path):
    from agentl.connectors.files import json_write_chunks

    assert json_write_chunks(iter([[{"a": 1}], [{"a": 2}]]), str(tmp_path / "o.jsonl"))[0]["written"] == 2
    assert (tmp_path / "o.jsonl").read_text() == '{"a": 1}\n{"a": 2}\n'
    json_write_chunks(iter([[{"a": 1}], [{"a": 2}]]), str(tmp_path / "o.json"))
    assert '"a": 2' in (tmp_path / "o.json").read_text()


PG = os.environ.get("AGENTL_TEST_PG_DSN")


@pytest.mark.skipif(not PG, reason="set AGENTL_TEST_PG_DSN to run Postgres tests")
def test_postgres_streams_both_ways(tmp_path):
    from agentl.connectors.postgres import postgres_read_chunks, postgres_write_chunks

    chunks = [[{"id": i, "v": "x"} for i in range(s, s + 3)] for s in (0, 3, 6)]
    chunks[2][0]["extra"] = 1.5
    out = postgres_write_chunks(iter(chunks), dsn=PG, table="agentl_chunks", mode="replace")
    assert out[0]["written"] == 9
    got = list(postgres_read_chunks(chunk_size=4, dsn=PG, query="select * from agentl_chunks order by id"))
    assert [len(c) for c in got] == [4, 4, 1]
    assert got[2][0]["extra"] is None and got[1][2]["extra"] == 1.5
