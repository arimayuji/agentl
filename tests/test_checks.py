"""Data checks (#85): fixed expectations, comparison with the last run, agent review."""

import datetime as dt
import json
from typing import ClassVar

import pytest

from agentl import parse
from agentl.checks import parse_duration, parse_percent, profile, run_expectations
from agentl.llm import Result, register_provider
from agentl.runner import Runner
from agentl.store import Store

NOW = dt.datetime(2026, 10, 14, 9, 0, tzinfo=dt.timezone.utc)


def sales(n=10, **override):
    rows = [
        {"order_id": i, "region": "south" if i % 2 else "north", "total": 100 + i, "date": "2026-10-14"}
        for i in range(n)
    ]
    for i, patch in override.items():
        rows[int(i[1:])].update(patch)
    return rows


# ---------------------------------------------------------------- expectations
def test_clean_data_passes_every_expectation():
    expect = {
        "rows": {"min": 5, "max": 50},
        "not_null": ["order_id", "total"],
        "unique": "order_id",
        "accepted": {"region": ["north", "south"]},
        "range": {"total": {"min": 0, "max": 1000}},
        "fresh": {"field": "date", "max_age": "1d"},
    }
    assert run_expectations(sales(), expect, None, NOW) == []


def test_each_expectation_explains_what_failed():
    rows = sales(10, r1={"total": None}, r2={"order_id": 0}, r3={"region": "west"}, r4={"total": -5})
    for r in rows:
        r["date"] = "2026-10-10"
    fails = run_expectations(
        rows,
        {
            "rows": {"min": 20},
            "not_null": ["total"],
            "unique": ["order_id"],
            "accepted": {"region": ["north", "south"]},
            "range": {"total": {"min": 0}},
            "fresh": {"field": "date", "max_age": "1d"},
        },
        None,
        NOW,
    )
    assert [f["check"] for f in fails] == ["rows", "not_null", "unique", "accepted", "range", "fresh"]
    assert fails[0]["detail"] == "10 rows, expected at least 20"
    assert fails[1]["detail"] == "total: 1 empty value"
    assert "duplicate" in fails[2]["detail"] and "'west'" in fails[3]["detail"]
    assert "below 0 (lowest -5.0)" in fails[4]["detail"]


def test_vs_last_run_compares_rows_and_sums():
    before = profile(sales(100))
    assert run_expectations(sales(95), {"vs_last_run": {"rows": "10%"}}, before) == []
    fails = run_expectations(sales(50), {"vs_last_run": {"rows": "10%", "sum": {"total": "20%"}}}, before)
    assert [f["check"] for f in fails] == ["vs_last_run", "vs_last_run"]
    assert "-50%" in fails[0]["detail"] and "sum of total" in fails[1]["detail"]
    assert run_expectations(sales(1), {"vs_last_run": {"rows": "10%"}}, None) == []  # first run


def test_profile_is_small_and_readable():
    p = profile(sales(200))
    assert p["rows"] == 200 and len(p["sample"]) == 15
    assert p["columns"]["total"] == {
        "nulls": 0,
        "distinct": 200,
        "min": 100.0,
        "max": 299.0,
        "mean": 199.5,
        "sum": 39900.0,
    }
    assert p["columns"]["region"]["top"] == [["north", 100], ["south", 100]]


def test_duration_and_percent_parsing():
    assert parse_duration("6h") == 21600 and parse_duration("1d") == 86400 and parse_duration(30) == 30
    assert parse_percent("30%") == 0.3 and parse_percent(0.5) == 0.5
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_spec_errors():
    base = {"name": "x", "steps": [{"id": "src", "fn": "tests.test_checks:src"}]}
    with pytest.raises(ValueError, match="needs 'expect', 'review' or 'on_drift'"):
        parse({**base, "steps": [*base["steps"], {"id": "c", "check": "src"}]})
    with pytest.raises(ValueError, match="unknown expectation"):
        parse({**base, "steps": [*base["steps"], {"id": "c", "check": "src", "expect": {"nope": 1}}]})
    with pytest.raises(ValueError, match="belong to check steps"):
        parse(
            {**base, "steps": [{"id": "src", "fn": "tests.test_checks:src", "expect": {"rows": {"min": 1}}}]}
        )


# ---------------------------------------------------------------- runner
DATA = {"rows": sales(10)}


def src(input=None):
    return DATA["rows"]


SAVED: list = []


def save(input):
    SAVED.append(len(input))
    return [{"written": len(input)}]


class Reviewer:
    VERDICT: ClassVar[dict] = {"passed": True, "findings": []}
    SEEN: ClassVar[list] = []

    def __init__(self, spec):
        pass

    def structured(self, instruction, record, schema):
        self.SEEN.append(record)
        return Result(schema.model_validate(self.VERDICT), 300, 40)

    def text(self, instruction, content):
        return ""


register_provider("reviewer", Reviewer)


def pipeline(expect=None, review=None, on_fail="stop"):
    check = {"id": "verify", "check": "src", "on_fail": on_fail}
    if expect:
        check["expect"] = expect
    if review:
        check["review"] = review
    return parse(
        {
            "name": "daily-sales",
            "llm": {"provider": "reviewer", "model": "m", "price": {"input": 1, "output": 1}},
            "steps": [
                {"id": "src", "fn": "tests.test_checks:src"},
                check,
                {"id": "save", "fn": "tests.test_checks:save", "input": "verify"},
            ],
        }
    )


@pytest.fixture(autouse=True)
def reset():
    DATA["rows"] = sales(10)
    SAVED.clear()
    Reviewer.SEEN.clear()
    Reviewer.VERDICT = {"passed": True, "findings": []}


def run(tmp_path, spec):
    with Store(tmp_path / "s.db") as store:
        r = Runner(spec, store=store).run()
        return r, [e for e in store.events(r.run_id) if e["type"].startswith("check.")]


def test_failing_check_stops_before_the_writer(tmp_path):
    DATA["rows"] = sales(3)
    r, events = run(tmp_path, pipeline({"rows": {"min": 5}}))
    assert r.status == "failed" and "3 rows, expected at least 5" in r.error
    assert SAVED == [] and events[0]["type"] == "check.failed"
    assert events[0]["data"]["findings"] == [{"check": "rows", "detail": "3 rows, expected at least 5"}]


def test_warn_lets_the_data_through(tmp_path):
    DATA["rows"] = sales(3)
    r, events = run(tmp_path, pipeline({"rows": {"min": 5}}, on_fail="warn"))
    assert r.status == "succeeded" and SAVED == [3] and events[0]["type"] == "check.failed"


def test_passing_check_passes_records_through_and_remembers_the_run(tmp_path):
    spec = pipeline({"vs_last_run": {"rows": "20%"}})
    r, events = run(tmp_path, spec)
    assert (
        r.status == "succeeded" and r.outputs["verify"] == sales(10) and events[0]["type"] == "check.passed"
    )
    DATA["rows"] = sales(5)  # half the volume of the last delivered run
    r, events = run(tmp_path, spec)
    assert r.status == "failed" and "-50%" in r.error


def test_agent_review_reads_the_profile_and_can_block(tmp_path):
    Reviewer.VERDICT = {"passed": False, "findings": ["The north region has no orders today."]}
    r, events = run(tmp_path, pipeline({"rows": {"min": 1}}, review="Daily sales for finance."))
    assert r.status == "failed" and "north region" in r.error
    seen = Reviewer.SEEN[0]
    assert seen["today"]["rows"] == 10 and "sample" in seen["today"] and seen["failed_fixed_checks"] == []
    assert r.spend["verify"]["llm_calls"] == 1 and r.spend["verify"]["input_tokens"] == 300
    assert events[0]["data"]["findings"][0]["check"] == "review"


def test_review_notes_on_a_pass_are_reported(tmp_path):
    Reviewer.VERDICT = {"passed": True, "findings": ["Totals are 5% above last week, within normal."]}
    r, events = run(tmp_path, pipeline(review="Daily sales."))
    assert r.status == "succeeded" and events[0]["data"]["notes"] == [
        "Totals are 5% above last week, within normal."
    ]


def test_check_shows_up_in_the_graph():
    from agentl.dashboard import parse_graph
    from agentl.graph import mermaid

    nodes = {n["id"]: n["kind"] for n in parse_graph(mermaid(pipeline({"rows": {"min": 1}})))["nodes"]}
    assert nodes == {"src": "fn", "verify": "check", "save": "fn"}
    assert json.dumps(nodes)
