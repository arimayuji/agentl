"""first_valid (#195): deterministic parsers first, an agent last, and which path ran."""

import pytest

from agentl import dashboard, parse, register_provider
from agentl.llm import Result
from agentl.runner import Runner
from agentl.spec import SpecError
from agentl.store import Store

ASKED: list[dict] = []


class Fake:
    def __init__(self, spec):
        pass

    def structured(self, instruction, record, schema):
        ASKED.append(record)
        return Result(schema.model_validate({"date": "2026-01-02", "value": 7.5}), 10, 2)

    def text(self, instruction, content):
        return ""


register_provider("first-valid-fake", Fake)

DOC = [{"text": "date;value\n2026-01-01;1.5\n2026-01-02;2.5"}]


def doc(input=None):
    return [dict(r) for r in DOC]


def parse_v2(records):
    raise ValueError("layout v2 not found")


def parse_v1(records):
    lines = records[0]["text"].splitlines()[1:]
    return [{"date": d, "value": float(v)} for d, v in (line.split(";") for line in lines)]


def garbage(records):
    return [{"date": "2026-01-01", "value": "n/a"}]


def empty(records):
    return []


def pipeline(alternatives, **fv):
    return parse(
        {
            "name": "fv",
            "llm": {"default": {"provider": "first-valid-fake", "model": "m"}},
            "steps": [
                {"id": "doc", "fn": "tests.test_first_valid:doc"},
                {
                    "id": "rows",
                    "input": "doc",
                    "first_valid": {
                        "validate": {
                            "rows_min": 1,
                            "required": ["date", "value"],
                            "types": {"value": "float"},
                        },
                        "try": alternatives,
                        **fv,
                    },
                },
            ],
        }
    )


AGENT = {"agent": "Extract the table's rows.", "output": {"date": "str", "value": "float"}}


def run(spec, tmp_path, store=None):
    ASKED.clear()
    events: list[dict] = []
    res = Runner(spec, store or Store(tmp_path / "s.db"), listeners=[events.append]).run()
    return res, events


def test_an_error_falls_through_to_the_next_parser_and_tags_the_path(tmp_path):
    spec = pipeline(
        [{"fn": "tests.test_first_valid:parse_v2"}, {"fn": "tests.test_first_valid:parse_v1"}, AGENT]
    )
    res, events = run(spec, tmp_path)
    assert res.status == "succeeded"
    assert res.outputs["rows"] == [
        {"date": "2026-01-01", "value": 1.5, "_path": "tests.test_first_valid:parse_v1"},
        {"date": "2026-01-02", "value": 2.5, "_path": "tests.test_first_valid:parse_v1"},
    ]
    assert ASKED == []
    fell = [e for e in events if e["type"] == "first_valid.fallthrough"]
    assert [e["data"]["path"] for e in fell] == ["tests.test_first_valid:parse_v2"]
    assert "layout v2 not found" in fell[0]["data"]["error"]
    won = next(e for e in events if e["type"] == "first_valid.path")
    assert won["data"]["index"] == 1 and not won["data"]["agent"]
    assert res.spend["rows"]["paths"] == {"tests.test_first_valid:parse_v1": 2}


def test_invalid_output_falls_through_to_the_agent(tmp_path):
    spec = pipeline(
        [
            {"fn": "tests.test_first_valid:garbage", "name": "v2"},
            {"fn": "tests.test_first_valid:empty", "name": "v1"},
            AGENT,
        ]
    )
    res, events = run(spec, tmp_path)
    assert res.status == "succeeded"
    assert res.outputs["rows"][0]["_path"] == "agent"
    assert res.outputs["rows"][0]["value"] == 7.5
    assert len(ASKED) == 1 and "text" in ASKED[0]
    errors = {e["data"]["path"]: e["data"]["error"] for e in events if e["type"] == "first_valid.fallthrough"}
    assert "type check failed" in errors["v2"]
    assert "rows_min" in errors["v1"]
    assert res.spend["rows"]["llm_calls"] == 1


def test_required_field_missing_falls_through(tmp_path):
    spec = pipeline([{"fn": "tests.test_first_valid:doc"}, AGENT])  # no date, no value
    res, _ = run(spec, tmp_path)
    assert res.outputs["rows"][0]["_path"] == "agent"


def test_every_alternative_failing_fails_the_step_with_each_reason(tmp_path):
    spec = pipeline([{"fn": "tests.test_first_valid:parse_v2"}, {"fn": "tests.test_first_valid:empty"}])
    res, _ = run(spec, tmp_path)
    assert res.status == "failed"
    assert "no first_valid alternative passed" in res.error
    assert "layout v2 not found" in res.error and "rows_min" in res.error


def test_report_path_false_leaves_records_untagged(tmp_path):
    spec = pipeline([{"fn": "tests.test_first_valid:parse_v1"}], report_path=False)
    res, _ = run(spec, tmp_path)
    assert all("_path" not in r for r in res.outputs["rows"])


def test_agent_share_alert_and_dashboard_shares(tmp_path):
    store = Store(tmp_path / "s.db")
    good = pipeline([{"fn": "tests.test_first_valid:parse_v1", "name": "v1"}, AGENT], alert_agent_share=0.5)
    bad = pipeline([{"fn": "tests.test_first_valid:empty", "name": "v1"}, AGENT], alert_agent_share=0.5)
    _, e1 = run(good, tmp_path, store)
    _, e2 = run(bad, tmp_path, store)  # 1 of 2 runs: not above 50%
    assert not [e for e in e1 + e2 if e["type"] == "first_valid.alert"]
    res, e3 = run(bad, tmp_path, store)  # 2 of 3
    alert = next(e for e in e3 if e["type"] == "first_valid.alert")
    assert alert["data"]["agent_share"] == pytest.approx(0.667, abs=1e-3) and alert["data"]["runs"] == 3
    detail = dashboard.run_detail(store, res.run_id)
    shares = {p["path"]: (p["count"], p["runs"]) for p in detail["paths"] if p["step"] == "rows"}
    assert shares == {"agent": (2, 3), "v1": (1, 3)}


@pytest.mark.parametrize(
    "alternatives, fv, message",
    [
        ([{"fn": "a:b", "use": "x"}], {}, "exactly one of fn, use or agent"),
        ([{"agent": "Extract."}], {}, "needs 'output:'"),
        ([{"fn": "a:b", "output": {"x": "str"}}], {}, "belong to the agent alternative"),
        ([{"fn": "a:b"}, {"fn": "a:b"}], {}, "distinct labels"),
        ([{"fn": "a:b"}], {"alert_agent_share": 0.2}, "needs an agent alternative"),
        ([], {}, "at least 1"),
    ],
)
def test_spec_errors(alternatives, fv, message):
    with pytest.raises(SpecError, match=message):
        pipeline(alternatives, **fv)


def test_unknown_llm_on_the_agent_alternative():
    with pytest.raises(SpecError, match="unknown llm 'nope'"):
        pipeline([{**AGENT, "llm": "nope"}])


def test_step_level_with_or_output_is_rejected():
    with pytest.raises(SpecError, match="per alternative"):
        parse(
            {
                "name": "fv",
                "steps": [
                    {"id": "doc", "fn": "tests.test_first_valid:doc"},
                    {
                        "id": "rows",
                        "input": "doc",
                        "output": {"x": "str"},
                        "first_valid": {"try": [{"fn": "a:b"}]},
                    },
                ],
            }
        )
