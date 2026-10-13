import datetime as dt
import sys
import types

import pytest

pd = pytest.importorskip("pandas")

from agentl import parse  # noqa: E402
from agentl.pandas import dataframe, to_df, to_records  # noqa: E402
from agentl.runner import Runner  # noqa: E402
from agentl.store import Store  # noqa: E402


def test_round_trip_cleans_values():
    df = pd.DataFrame(
        {
            "id": [1, 2],
            "total": [1.5, float("nan")],
            "when": pd.to_datetime(["2026-01-01", None]),
            "tag": pd.array(["a", None], dtype="string"),
        }
    )
    rows = to_records(df)
    assert rows[0] == {"id": 1, "total": 1.5, "when": dt.datetime(2026, 1, 1), "tag": "a"}
    assert rows[1] == {"id": 2, "total": None, "when": None, "tag": None}
    assert type(rows[0]["id"]) is int
    assert list(to_df(rows).columns) == ["id", "total", "when", "tag"]


def test_groupby_index_becomes_columns():
    df = pd.DataFrame({"k": ["a", "a", "b"], "v": [1, 2, 3]}).groupby("k").v.sum()
    assert to_records(df) == [{"k": "a", "v": 3}, {"k": "b", "v": 3}]


def test_decorator_single_and_multi_input():
    @dataframe
    def double(df, factor=2):
        return df.assign(v=df.v * factor)

    assert double([{"v": 1}], factor=3) == [{"v": 3}]
    assert dataframe(lambda df: df)([]) == []

    @dataframe
    def merge(dfs):
        return dfs["a"].merge(dfs["b"], on="id")

    assert merge({"a": [{"id": 1, "x": 1}], "b": [{"id": 1, "y": 2}]}) == [{"id": 1, "x": 1, "y": 2}]

    with pytest.raises(TypeError, match="DataFrame"):
        dataframe(lambda df: 1)([{"v": 1}])


def test_in_pipeline(tmp_path, monkeypatch):
    @dataframe
    def top(df, n=1):
        return df.astype({"v": int}).nlargest(n, "v")

    monkeypatch.setitem(sys.modules, "my_steps", types.SimpleNamespace(top=top))
    (tmp_path / "in.csv").write_text("id,v\n1,5\n2,9\n3,7\n")
    spec = parse(
        {
            "name": "p",
            "steps": [
                {"id": "raw", "use": "csv.read", "with": {"path": str(tmp_path / "in.csv")}},
                {"id": "cast", "fn": "my_steps:top", "input": "raw", "with": {"n": 2}},
            ],
        }
    )
    with Store(tmp_path / "db.sqlite") as store:
        result = Runner(spec, store=store).run()
    assert result.status == "succeeded", result.error
    assert [r["id"] for r in result.outputs["cast"]] == ["2", "3"]
