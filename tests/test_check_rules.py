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


# ---------------------------------------------------------------- scheduled digest
import datetime as dt  # noqa: E402

from agentl import digest as dg  # noqa: E402
from agentl.notify import register_channel  # noqa: E402

SENT: list = []


def _fake(cfg, text, event):
    if cfg.get("broken"):
        raise RuntimeError("channel down")
    SENT.append((cfg.get("name"), text, event))


register_channel("fake_digest", _fake)


def digest_spec(rules, **digest):
    s = spec(rules, identity=["id"]).model_dump(exclude_unset=True)
    s["notify"] = {
        "channels": {
            "a": {"type": "fake_digest", "name": "a"},
            "b": {"type": "fake_digest", "name": "b", "broken": digest.pop("broken", False)},
        },
        "digest": {"to": ["a", "b"], "at": "08:00", **digest},
    }
    return parse(s)


def at(day, hour, minute=0):
    return dt.datetime(2026, 1, day, hour, minute, tzinfo=dt.timezone.utc)


@pytest.fixture
def sent():
    SENT.clear()
    return SENT


def test_digest_is_sent_once_per_day_after_at(tmp_path, sent):
    s = digest_spec([RANGE])
    DATA["rows"] = [{"id": 1, "amount": -1}]
    run(tmp_path, s)
    with Store(tmp_path / "s.db") as store:
        assert dg.send_due(s, store, at(1, 7, 59)) == {"a": "not due", "b": "not due"}
        assert dg.send_due(s, store, at(1, 8)) == {"a": "sent", "b": "sent"}
        assert dg.send_due(s, store, at(1, 9)) == {"a": "already sent", "b": "already sent"}
        assert dg.send_due(s, store, at(2, 7)) == {"a": "not due", "b": "not due"}
    assert len(sent) == 2
    text = sent[0][1]
    assert "digest for 2026-01-01" in text and "verify: new 1, resolved 0, ongoing 0" in text
    assert "between|amount|id=1" in text and sent[0][2]["type"] == "check.digest.daily"


def test_guard_is_written_only_after_a_successful_send(tmp_path, sent):
    s = digest_spec([RANGE], broken=True)
    run(tmp_path, s)
    with Store(tmp_path / "s.db") as store:
        out = dg.send_due(s, store, at(1, 8))
        assert out["a"] == "sent" and out["b"].startswith("failed")
        out = dg.send_due(s, store, at(1, 8, 5))  # the retry goes only to the failed channel
        assert out["a"] == "already sent" and out["b"].startswith("failed")
        assert store.cache_get(dg.guard_key("daily-sales", "b")) is None


def test_at_most_once_writes_the_guard_first(tmp_path, sent):
    s = digest_spec([RANGE], broken=True, idempotent=False)
    run(tmp_path, s)
    with Store(tmp_path / "s.db") as store:
        dg.send_due(s, store, at(1, 8))
        assert dg.send_due(s, store, at(1, 9))["b"] == "already sent"


def test_heartbeat_on_a_clean_day_and_a_missing_run(tmp_path, sent):
    s = digest_spec([RANGE])
    DATA["rows"] = [{"id": 1, "amount": 5}]
    run(tmp_path, s)
    with Store(tmp_path / "s.db") as store:
        dg.send_due(s, store, at(1, 8))
        assert "all clear: no findings today" in sent[0][1] and sent[0][2]["data"]["all_clear"]
        quiet = digest_spec([RANGE], heartbeat=False)
        store.cache_clear("notify-digest:*")
        assert dg.send_due(quiet, store, at(1, 8))["a"] == "all clear (no heartbeat)"
        sent.clear()
        dg.send_due(quiet, store, at(2, 8))  # no run on the 2nd: that is never "clean"
        assert "verify: no run today (last on 2026-01-01)" in sent[0][1]


def test_resolved_since_yesterday_and_sections(tmp_path, sent, monkeypatch):
    s = digest_spec([RANGE], sections=["resolved"])
    DATA["rows"] = [{"id": 1, "amount": -1}]
    run(tmp_path, s)
    monkeypatch.setattr(runner, "_today", lambda: "2026-01-02")
    DATA["rows"] = [{"id": 1, "amount": 1}]
    run(tmp_path, s)
    with Store(tmp_path / "s.db") as store:
        dg.send_due(s, store, at(2, 8))
    text = sent[0][1]
    assert "verify: resolved 1" in text and "new" not in text.split("\n", 1)[1]


def test_every_and_timezone(tmp_path, sent):
    s = digest_spec([RANGE], every="7d", tz="America/Sao_Paulo")
    run(tmp_path, s)
    with Store(tmp_path / "s.db") as store:
        assert dg.send_due(s, store, at(1, 10))["a"] == "not due"  # 07:00 in Sao Paulo
        assert dg.send_due(s, store, at(1, 11))["a"] == "sent"
        assert dg.send_due(s, store, at(7, 12))["a"] == "already sent"  # this week's went out
        assert dg.send_due(s, store, at(8, 12))["a"] == "sent"
        assert dg.send_due(s, store, at(8, 13), force=True)["a"] == "sent"


def test_rule_notify_none_acts_silently(tmp_path):
    silent = {**RANGE, "notify": "none"}
    DATA["rows"] = [{"id": 1, "amount": -1}]
    r, events = run(tmp_path, spec([silent], identity=["id"]))
    assert SAVED == [[]] and r.rejected["verify"][0]["id"] == 1  # still dropped
    assert events[0]["data"]["all_clear"] and events[0]["data"]["new"] == 0


def test_digest_spec_validation():
    with pytest.raises(ValueError, match="unknown channel"):
        parse({"name": "x", "steps": [], "notify": {"digest": {"to": ["slack"]}}})
    bad_values = [({"every": "12h"}, "whole days"), ({"at": "8am"}, "HH:MM"), ({"tz": "Mars/X"}, "time zone")]
    for bad, msg in bad_values:
        with pytest.raises(ValueError, match=msg):
            digest_spec([RANGE], **bad)


def test_cli_digest(tmp_path, capsys, monkeypatch):
    import yaml

    from agentl.cli import main

    s = digest_spec([RANGE], broken=True).model_dump(exclude_unset=True)
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(s))
    db = str(tmp_path / "s.db")
    assert main(["digest", str(path), "--db", db, "--show"]) == 0
    assert "verify: no run today (never ran)" in capsys.readouterr().out
    assert main(["digest", str(path), "--db", db, "--force"]) == 1  # channel b is down
    assert "a: sent" in capsys.readouterr().out


def test_serve_tick_sends_the_digest_once(tmp_path, sent):
    from agentl.triggers import Service

    s = digest_spec([RANGE])
    run(tmp_path, s)
    s = parse({**s.model_dump(exclude_unset=True), "trigger": [{"webhook": "/sales"}]})
    with Store(tmp_path / "s.db") as store:
        service = Service([s], store)
        service.tick(at(1, 8).timestamp())
        service.tick(at(1, 8, 2).timestamp())
        service.next_digest.clear()
        service.tick(at(1, 8, 3).timestamp())
    assert [name for name, _, _ in sent] == ["a", "b"]
