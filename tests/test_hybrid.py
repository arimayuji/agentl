"""Hybrid agent steps (#124, #195): rules first, the llm only for the rows they cannot handle."""

import pytest

from agentl import parse, register_provider
from agentl.llm import Result
from agentl.runner import Runner
from agentl.spec import SpecError
from agentl.store import Store

ASKED: list[str] = []


class Fake:
    def __init__(self, spec):
        pass

    def structured(self, instruction, record, schema):
        ASKED.append(record["address"])
        return Result(schema.model_validate({"zip": "00000", "state": "NY"}), 10, 2)

    def text(self, instruction, content):
        return ""


register_provider("hybrid-fake", Fake)

ADDRESSES = [
    "1 Main St, Springfield, IL 62701",
    "somewhere near the old mill",  # no rule matches
    "5 Oak Ave, Austin, TX 73301",
    "9 Pine Rd, Denver, ZZ 80201",  # ZZ is not a state: fails the cast
    "2 Elm St, Boston, MA 02108",
]


def rows(input=None):
    return [{"id": i, "address": a} for i, a in enumerate(ADDRESSES)]


def upper_state(rec):
    if "near" in rec["address"]:
        raise ValueError("no layout")
    return {"note": "parsed"}


def pipeline(rules, **extra):
    return parse(
        {
            "name": "hybrid",
            "llm": {"default": {"provider": "hybrid-fake", "model": "m"}},
            "steps": [
                {"id": "src", "fn": "tests.test_hybrid:rows"},
                {
                    "id": "parse",
                    "input": "src",
                    "rules": rules,
                    "agent": "Extract the zip code and the state.",
                    "output": {"zip": "str", "state": "str"},
                    **extra,
                },
            ],
        }
    )


RULES = [
    {
        "compute": {
            "zip": "regex(address, '\\d{5}$')",
            "state": "regex(address, '[A-Z]{2}(?= \\d{5}$)')",
        }
    },
    {"cast": {"state": "state"}},
]


def run(spec, tmp_path):
    ASKED.clear()
    return Runner(spec, Store(tmp_path / "s.db")).run()


def test_clean_rows_never_reach_the_llm_and_order_is_kept(tmp_path):
    events: list[dict] = []
    spec = pipeline(RULES)
    ASKED.clear()
    res = Runner(spec, Store(tmp_path / "s.db"), listeners=[events.append]).run()
    assert res.status == "succeeded"
    out = res.outputs["parse"]
    assert [r["id"] for r in out] == [0, 1, 2, 3, 4]
    assert [r["_path"] for r in out] == ["rules", "agent", "rules", "agent", "rules"]
    assert out[0]["zip"] == "62701" and out[0]["state"] == "IL"
    assert out[1]["zip"] == "00000"
    assert sorted(ASKED) == sorted([ADDRESSES[1], ADDRESSES[3]])
    assert res.llm_calls == 2
    assert res.spend["parse"]["paths"] == {"rules": 3, "agent": 2}
    assert res.summary()["spend"]["parse"]["paths"] == {"rules": 3, "agent": 2}
    paths = [e for e in events if e["type"] == "hybrid.paths"]
    assert paths and paths[0]["data"]["rules"] == 3 and paths[0]["data"]["agent"] == 2


def test_all_clean_makes_zero_calls(tmp_path):
    spec = pipeline([{"compute": {"zip": "'12345'", "state": "'NY'"}}])
    res = run(spec, tmp_path)
    assert ASKED == [] and res.llm_calls == 0
    assert {r["_path"] for r in res.outputs["parse"]} == {"rules"}


def test_fn_rule_that_raises_falls_through(tmp_path):
    rules = [{"fn": "tests.test_hybrid:upper_state"}, *RULES]
    res = run(pipeline(rules), tmp_path)
    assert sorted(ASKED) == sorted([ADDRESSES[1], ADDRESSES[3]])
    assert "note" not in res.outputs["parse"][0]  # only the output fields are kept


def test_rule_value_must_match_output_type(tmp_path):
    spec = pipeline([{"compute": {"zip": "5", "state": "'NY'"}}])  # int where str is expected
    res = run(spec, tmp_path)
    assert res.llm_calls == 5


@pytest.mark.parametrize(
    "step",
    [
        {"use": "select", "rules": [{"compute": {"a": "1"}}]},
        {"agent": "x", "output": {"a": "str"}, "rules": [{"sql": "x"}]},
        {"agent": "x", "output": {"a": "str"}, "rules": [{"fn": "nocolon"}]},
        {"agent": "x", "output": {"a": "str"}, "rules": [{"compute": {"a": "().__class__"}}]},
    ],
)
def test_bad_rules_fail_at_parse(step):
    with pytest.raises((SpecError, ValueError)):
        parse({"name": "x", "steps": [{"id": "s", **step}]})
