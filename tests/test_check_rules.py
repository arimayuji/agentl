"""Per-row check rules and the daily digest (#218)."""

import pytest

from agentl import parse, runner
from agentl.checks import digest, run_rules
from agentl.runner import Runner
from agentl.spec import RowRuleSpec
from agentl.store import Store

DATA: dict = {"rows": []}
SAVED: list = []


def src(input):
    return [dict(r) for r in DATA["rows"]]


def save(input):
    SAVED.append(list(input))
    return input


def spec(rules, identity=None):
    check = {"id": "verify", "check": "src", "rules": rules}
    if identity:
        check["identity"] = identity
    return parse(
        {
            "name": "daily-sales",
            "steps": [
                {"id": "src", "fn": "tests.test_check_rules:src"},
                check,
                {"id": "save", "fn": "tests.test_check_rules:save", "input": "verify"},
            ],
        }
    )


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    SAVED.clear()
    DATA["rows"] = []
    monkeypatch.setattr(runner, "_today", lambda: "2026-01-01")


def run(tmp_path, s):
    with Store(tmp_path / "s.db") as store:
        r = Runner(s, store=store).run()
        return r, [e for e in store.events(r.run_id) if e["type"] == "check.digest"]


RANGE = {"rule": "between", "column": "amount", "min": 0, "max": 100, "on_fail": "drop"}
DIFF = {"rule": "abs_diff_max", "column": "amount", "other": "billed", "max": 5}


def test_drop_flag_and_rule_errors_never_break_the_flow():
    rules = [RowRuleSpec(**RANGE), RowRuleSpec(**DIFF)]
    rows = [{"amount": 10, "billed": 10}, {"amount": 500, "billed": 500}, {"amount": 20, "billed": 90}]
    rows.append({"amount": "n/a", "billed": 1})
    kept, found, errors = run_rules(rows, rules)
    assert [r["amount"] for r in kept] == [10, 20, "n/a"]
    assert [(f["rule"], f["action"]) for f in found] == [("between", "drop"), ("abs_diff_max", "flag")]
    assert len(errors) == 2  # "n/a" is not a number for either rule: logged, row passes


def test_rule_spec_needs_its_settings():
    with pytest.raises(ValueError, match="other and max"):
        RowRuleSpec(rule="abs_diff_max", column="a", max=1)


def test_digest_new_resolved_ongoing():
    assert digest(["a", "b"], ["b", "c"]) == {"new": ["a"], "resolved": ["c"], "ongoing": ["b"]}


def test_runner_drops_rows_and_emits_a_digest(tmp_path):
    DATA["rows"] = [{"id": 1, "amount": 10}, {"id": 2, "amount": -1}]
    r, events = run(tmp_path, spec([RANGE], identity=["id"]))
    assert r.status == "succeeded" and SAVED == [[{"id": 1, "amount": 10}]]
    assert r.rejected["verify"][0]["id"] == 2
    d = events[0]["data"]
    assert d["new"] == 1 and d["dropped"] == 1 and not d["all_clear"]
    assert d["sections"]["new"] == ["between|amount|id=2"]


def test_fail_rule_stops_the_step(tmp_path):
    DATA["rows"] = [{"amount": -1}]
    r, events = run(tmp_path, spec([{**RANGE, "on_fail": "fail"}]))
    assert r.status == "failed" and SAVED == [] and events


def test_heartbeat_and_resolved_since_yesterday(tmp_path, monkeypatch):
    s = spec([RANGE], identity=["id"])
    DATA["rows"] = [{"id": 1, "amount": -1}]
    run(tmp_path, s)
    monkeypatch.setattr(runner, "_today", lambda: "2026-01-02")
    DATA["rows"] = [{"id": 1, "amount": 1}]
    _, events = run(tmp_path, s)
    d = events[0]["data"]
    assert d["resolved"] == 1 and d["all_clear"] and d["sections"]["resolved"] == ["between|amount|id=1"]
    monkeypatch.setattr(runner, "_today", lambda: "2026-01-03")
    _, events = run(tmp_path, s)
    assert events[0]["data"]["summary"] == "all clear: no findings today"


def test_same_day_runs_share_a_bucket(tmp_path):
    s = spec([RANGE], identity=["id"])
    DATA["rows"] = [{"id": 1, "amount": -1}]
    run(tmp_path, s)
    DATA["rows"] = [{"id": 2, "amount": -1}]
    _, events = run(tmp_path, s)
    assert events[0]["data"]["new"] == 2


def test_rules_meaning_depends_on_the_step_kind():
    from agentl import parse
    from agentl.spec import RowRuleSpec

    src = {"id": "src", "fn": "tests.test_checks:src"}
    check = {"id": "c", "check": "src", "rules": [{"rule": "not_null", "column": "id"}]}
    spec = parse({"name": "x", "steps": [src, check]})
    assert isinstance(spec.steps[1].rules[0], RowRuleSpec)

    agent = {"id": "a", "agent": "clean", "output": {"id": "str"}, "rules": [{"cast": {"id": "str"}}]}
    spec = parse({"name": "x", "steps": [src, agent]})
    assert spec.steps[1].rules == [{"cast": {"id": "str"}}]

    with pytest.raises(ValueError, match="hybrid rule of agent steps"):
        parse({"name": "x", "steps": [src, {**check, "rules": [{"cast": {"id": "str"}}]}]})
    with pytest.raises(ValueError, match=r"rules go on an agent step .* or on a check step"):
        parse({"name": "x", "steps": [{**src, "rules": [{"cast": {"id": "str"}}]}]})
    with pytest.raises(ValueError, match="identity belongs to check steps"):
        parse({"name": "x", "steps": [{**src, "identity": ["id"]}]})
