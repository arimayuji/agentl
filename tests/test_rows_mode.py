"""Compact rows output and continuation on truncation (#215)."""

import pytest

from agentl import parse, register_provider
from agentl.llm import RowsChunk
from agentl.runner import Runner, drop_overlap, parse_rows
from agentl.spec import SpecError
from agentl.store import Store

CALLS: list[int] = []


class Truncating:
    """First answer is cut mid-row; the continuation repeats the last complete row."""

    def __init__(self, spec):
        pass

    def rows(self, instruction, record, columns, previous):
        CALLS.append(len(previous))
        if not previous:
            return RowsChunk('["a", 1.5]\n["b", 2]\n["c", 3', True, 10, 20)
        return RowsChunk('["b", 2]\n["c", 3]\n["d", 4, "extra"]\n["e", "x"]\nnot json', False, 15, 10)

    def structured(self, instruction, record, schema):
        raise AssertionError("rows mode does not call structured")

    def text(self, instruction, content):
        return ""


register_provider("rows-fake", Truncating)


def src(input=None):
    return [{"doc": "page 1"}]


def pipeline(**step):
    return parse(
        {
            "name": "rows",
            "llm": {"default": {"provider": "rows-fake", "model": "m"}},
            "steps": [
                {"id": "src", "fn": "tests.test_rows_mode:src"},
                {
                    "id": "rows",
                    "input": "src",
                    "agent": "extract the items",
                    "output_mode": "rows",
                    "output": {"item": "str", "value": "float"},
                    **step,
                },
            ],
        }
    )


def test_continues_without_duplicates_and_rejects_bad_rows_one_by_one(tmp_path):
    CALLS.clear()
    res = Runner(pipeline(), store=Store(tmp_path / "s.db")).run()
    assert res.status == "succeeded", res.error
    assert CALLS == [0, 1]
    assert res.outputs["rows"] == [
        {"item": "a", "value": 1.5},
        {"item": "b", "value": 2.0},
        {"item": "c", "value": 3.0},
    ]
    errors = [r["_error"] for r in res.rejected["rows"]]
    assert len(errors) == 3
    assert "expected 2" in errors[1] or "expected 2" in errors[0]
    spend = res.spend["rows"]
    assert spend["continuations"] == 1
    assert spend["output_tokens_saved_est"] > 0
    assert spend["llm_calls"] == 2


def test_on_truncated_fail_rejects_the_record(tmp_path):
    res = Runner(pipeline(on_truncated="fail"), store=Store(tmp_path / "s.db")).run()
    assert res.outputs["rows"] == []
    assert "truncated" in res.rejected["rows"][0]["_error"]


def test_max_continuations_zero(tmp_path):
    res = Runner(pipeline(max_continuations=0), store=Store(tmp_path / "s.db")).run()
    assert "after 0 continuations" in res.rejected["rows"][0]["_error"]


def test_cache_hit_skips_the_call(tmp_path):
    store = Store(tmp_path / "s.db")
    Runner(pipeline(), store=store).run()
    CALLS.clear()
    res = Runner(pipeline(), store=store).run()
    assert CALLS == []
    assert len(res.outputs["rows"]) == 3


def test_echo_provider_rows(tmp_path):
    spec = parse(
        {
            "name": "echo",
            "llm": {"default": {"provider": "echo", "model": "x"}},
            "steps": [
                {"id": "src", "fn": "tests.test_rows_mode:src"},
                {
                    "id": "rows",
                    "input": "src",
                    "agent": "x",
                    "output_mode": "rows",
                    "output": {"doc": "str"},
                },
            ],
        }
    )
    res = Runner(spec, store=Store(tmp_path / "s.db")).run()
    assert res.outputs["rows"] == [{"doc": "page 1"}]


def test_parse_and_overlap_helpers():
    assert parse_rows("[1]\n[2", truncated=True) == ([[1]], [], "[1]")
    assert parse_rows("```json\n[1],\n[2]\n```") == ([[1], [2]], [], "[1],\n[2]")
    assert parse_rows("[1]\noops")[1] == ["oops"]
    assert drop_overlap([[1], [2]], [[2], [3]]) == [[3]]
    assert drop_overlap([[1]], [[2]]) == [[2]]


@pytest.mark.parametrize("extra", [{"records_per_call": 2}, {"mode": "batch"}, {"tools": ["http.get"]}])
def test_rows_mode_rejects_incompatible_options(extra):
    with pytest.raises((SpecError, ValueError), match="output_mode rows"):
        pipeline(**extra)
