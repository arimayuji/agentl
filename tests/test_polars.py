"""polars steps (#171): `agentl.polars.dataframe`, with records and on the Arrow path."""

import csv

import pytest

pl = pytest.importorskip("polars")
pq = pytest.importorskip("pyarrow.parquet")

from agentl import parse  # noqa: E402
from agentl.runner import Runner  # noqa: E402
from agentl.store import Store  # noqa: E402
from tests.arrow_steps import polars_steps as steps  # noqa: E402


def make_csv(path, n=50):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "kind", "amount", "note"])
        for i in range(n):
            w.writerow([i, "ab"[i % 2], f"{i * 1.37:.3f}", "" if i % 7 == 0 else f"n{i}"])
    return str(path)


def run(tmp_path, steps_, arrow):
    spec = parse({"name": "p", "arrow": arrow, "steps": steps_})
    with Store(tmp_path / f"state-{arrow}.db") as store:
        runner = Runner(spec, store=store)
        r = runner.run()
        assert r.status == "succeeded", r.error
        return r, runner


def chain(src, out, fn="clean"):
    return [
        {"id": "read", "use": "csv.read", "with": {"path": src}},
        {"id": "clean", "fn": f"tests.arrow_steps.polars_steps:{fn}", "input": "read"},
        {"id": "save", "use": "parquet.write", "input": "clean", "with": {"path": out}},
    ]


@pytest.fixture(autouse=True)
def reset():
    steps.CALLS.clear()


def test_arrow_path_matches_the_record_path(tmp_path):
    src = make_csv(tmp_path / "in.csv")
    slow, _ = run(tmp_path, chain(src, str(tmp_path / "slow.parquet")), False)
    fast, runner = run(tmp_path, chain(src, str(tmp_path / "fast.parquet")), True)
    assert runner._arrow_out == {"read", "clean"}
    assert steps.CALLS == ["DataFrame", "DataFrame"]
    a, b = pq.read_table(tmp_path / "fast.parquet"), pq.read_table(tmp_path / "slow.parquet")
    assert a.to_pylist() == b.to_pylist()
    assert fast.outputs["clean"] == slow.outputs["clean"]
    assert fast.outputs["clean"][0] == {"id": "0", "kind": "a", "amount": 0.0, "note": "", "missing": None}
    assert str(a.schema.field("note").type) == "string"  # not large_string / string_view


def test_lazyframe_is_collected(tmp_path):
    src = make_csv(tmp_path / "in.csv", n=6)
    for arrow in (False, True):
        r, _ = run(tmp_path, chain(src, str(tmp_path / f"l{arrow}.parquet"), "lazy"), arrow)
        assert r.outputs["clean"] == [{"id": str(i), "kind": "b"} for i in (1, 3, 5)]


def test_several_inputs_arrive_as_a_dict_of_frames(tmp_path):
    left, right = tmp_path / "l.csv", tmp_path / "r.csv"
    left.write_text("id,a\n1,x\n2,y\n")
    right.write_text("id,b\n2,z\n")
    r, _ = run(
        tmp_path,
        [
            {"id": "left", "use": "csv.read", "with": {"path": str(left)}},
            {"id": "right", "use": "csv.read", "with": {"path": str(right)}},
            {"id": "j", "fn": "tests.arrow_steps.polars_steps:join", "input": ["left", "right"]},
        ],
        None,
    )
    assert r.outputs["j"] == [{"id": "2", "a": "y", "b": "z"}]


def test_none_and_empty_input():
    from agentl.polars import dataframe

    assert dataframe(lambda df: None)([]) == []
    assert dataframe(lambda df: df)(None) == []
    with pytest.raises(TypeError, match="polars DataFrame"):
        dataframe(lambda df: [1])([{"a": 1}])
