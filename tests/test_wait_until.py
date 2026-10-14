"""wait_until: wait for a source that has no fixed publishing time (#88)."""

import datetime as dt

import pytest

from agentl import parse
from agentl.runner import Runner
from agentl.store import Store
from agentl.waits import holds, parse_condition

TODAY = dt.date(2026, 10, 14)


def test_conditions():
    rows = [{"date": "2026-10-13", "price": "5.9"}, {"date": "2026-10-14T06:30:00", "price": 6.1}]
    assert holds("rows > 0", rows) and not holds("rows > 0", [])
    assert holds("max(date) == today", rows, today=TODAY)
    assert not holds("max(date) == today", rows[:1], today=TODAY)
    assert holds("min(date) == yesterday", rows, today=TODAY)
    assert holds("max(price) >= 6", rows) and holds("sum(price) == 12", rows)
    assert holds("count(price) == 2", rows) and holds("max(date) > '2026-10-13'", rows)
    assert holds("changed", rows, last=None) and not holds("changed", [], last=None)
    with pytest.raises(ValueError, match="use 'rows > 0'"):
        parse_condition("whenever it feels right")


PORTAL = {"polls": 0, "ready_after": 3}


def portal(input=None):
    """Publishes today's file only from the Nth call on."""
    PORTAL["polls"] += 1
    day = dt.date.today() if PORTAL["polls"] >= PORTAL["ready_after"] else dt.date.today() - dt.timedelta(1)
    return [{"date": day.isoformat(), "total": 10}]


SAVED: list = []


def save(input):
    SAVED.append(input)
    return [{"written": len(input)}]


def pipeline(**wait):
    return parse(
        {
            "name": "portal-sync",
            "steps": [
                {"id": "portal", "fn": "tests.test_wait_until:portal"},
                {
                    "id": "wait_report",
                    "wait_until": {
                        "step": "portal",
                        "condition": "max(date) == today",
                        "every": "0.02s",
                        **wait,
                    },
                },
                {"id": "save", "fn": "tests.test_wait_until:save", "input": "wait_report"},
            ],
        }
    )


@pytest.fixture(autouse=True)
def reset():
    PORTAL.update(polls=0, ready_after=3)
    SAVED.clear()


def run(tmp_path, spec):
    with Store(tmp_path / "s.db") as store:
        r = Runner(spec, store=store).run()
        return r, [e for e in store.events(r.run_id) if e["type"].startswith("wait.")]


def test_polls_until_the_source_publishes(tmp_path):
    r, events = run(tmp_path, pipeline(timeout="5s"))
    assert r.status == "succeeded" and PORTAL["polls"] == 3
    assert [[{"date": dt.date.today().isoformat(), "total": 10}]] == SAVED
    assert [e["type"] for e in events] == ["wait.polled"] * 3 + ["wait.done"]
    assert [e["data"]["satisfied"] for e in events[:3]] == [False, False, True]


def test_timeout_fails_before_the_writer(tmp_path):
    PORTAL["ready_after"] = 999
    r, events = run(tmp_path, pipeline(timeout="0.1s"))
    assert r.status == "failed" and "did not hold for step 'portal'" in r.error
    assert SAVED == [] and events[-1]["type"] == "wait.timeout"


def test_timeout_can_continue_with_the_last_data(tmp_path):
    PORTAL["ready_after"] = 999
    r, events = run(tmp_path, pipeline(timeout="0.1s", on_timeout="continue"))
    assert r.status == "succeeded" and len(SAVED) == 1 and events[-1]["data"]["on_timeout"] == "continue"


def test_changed_waits_for_new_data(tmp_path):
    spec = pipeline(timeout="0.1s", condition="changed", on_timeout="continue")
    PORTAL["ready_after"] = 999
    _, events = run(tmp_path, spec)  # first run: anything counts as changed
    assert events[-1]["type"] == "wait.done"
    PORTAL["polls"] = 0
    _, events = run(tmp_path, spec)  # same data as the last good run: waits, then times out
    assert events[-1]["type"] == "wait.timeout"


def test_spec_errors():
    with pytest.raises(ValueError, match="drop 'input'"):
        parse(
            {
                "name": "x",
                "steps": [
                    {"id": "a", "fn": "tests.test_wait_until:portal"},
                    {"id": "w", "wait_until": {"step": "a"}, "input": "a"},
                ],
            }
        )
    with pytest.raises(ValueError, match="use 'rows > 0'"):
        parse(
            {
                "name": "x",
                "steps": [
                    {"id": "a", "fn": "tests.test_wait_until:portal"},
                    {"id": "w", "wait_until": {"step": "a", "condition": "soon"}},
                ],
            }
        )
