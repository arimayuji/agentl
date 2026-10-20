"""Run errors by fingerprint (#356): alert once, count repeats, remind, resolve, digest."""

import datetime as dt
import os

import pytest

from agentl import connector, errors, parse
from agentl import digest as dg
from agentl.notify import register_channel
from agentl.runner import Runner
from agentl.store import PostgresStore, Store

STATE: dict = {"error": None}
SENT: list = []


@connector("test.errors_src")
def _src(input):
    if STATE["error"] is not None:
        raise STATE["error"]
    return [{"n": 1}]


def _fake(cfg, text, event):
    SENT.append((event["type"], text, event))


register_channel("fake_errors", _fake)


class Clock:
    def __init__(self):
        self.t = dt.datetime(2026, 1, 1, 0, 0, tzinfo=dt.timezone.utc).timestamp()

    def __call__(self):
        return self.t

    def advance(self, hours):
        self.t += hours * 3600

    def dt(self):
        return dt.datetime.fromtimestamp(self.t, dt.timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(errors, "now", c)
    return c


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    STATE["error"] = None
    SENT.clear()
    if request.param == "sqlite":
        with Store(tmp_path / "e.db") as s:
            yield s
        return
    dsn = os.environ.get("AGENTL_TEST_PG_DSN")
    if not dsn:
        pytest.skip("set AGENTL_TEST_PG_DSN to run Postgres tests")
    with PostgresStore(dsn) as s:
        s._exec("DELETE FROM {errors} WHERE pipeline = 'orders'")
        s.cache_clear("notify-digest*")
        yield s


def spec(rule=None, digest=None):
    notify = {"channels": {"ops": {"type": "fake_errors"}}, "rules": [rule] if rule else []}
    if digest is not None:
        notify["digest"] = {"to": ["ops"], "at": "08:00", **digest}
    return parse({"name": "orders", "steps": [{"id": "load", "use": "test.errors_src"}], "notify": notify})


def fail(s, store, message):
    STATE["error"] = RuntimeError(message)
    return Runner(s, store=store).run()


def ok(s, store):
    STATE["error"] = None
    return Runner(s, store=store).run()


def sent_types():
    return [t for t, _, _ in SENT]


# ---------------------------------------------------------------- fingerprint
def test_fingerprint_ignores_numbers_ids_paths_and_timestamps():
    a = errors.fingerprint(
        "RuntimeError: row 12 of /data/in/2026-01-01.csv failed at 2026-01-01T03:04:05Z "
        "(run 3f2a9c1d-1111-2222-3333-444455556666)",
        "load",
    )
    b = errors.fingerprint(
        "RuntimeError: row 99 of /data/in/2026-02-09.csv failed at 2026-02-09T11:00:00Z "
        "(run 0a0b0c0d-aaaa-bbbb-cccc-ddddeeeeffff)",
        "load",
    )
    assert a == b and a.startswith("agentl-err-")
    assert errors.fingerprint("ValueError: row 12 failed", "load") != errors.fingerprint(
        "RuntimeError: row 12 failed", "load"
    )
    assert errors.fingerprint("RuntimeError: x", "load") != errors.fingerprint("RuntimeError: x", "save")
    assert errors.fingerprint("RuntimeError: timeout", "load") != errors.fingerprint(
        "RuntimeError: refused", "load"
    )
    assert errors.normalise("took 3.5s at 10:42 in ./out/a.json, id deadbeef01") == (
        "took <n>s at <ts> in <path>, id <id>"
    )


# ---------------------------------------------------------------- dedupe
def test_without_dedupe_every_failure_alerts(store, clock):
    s = spec({"on": "run.failed", "to": ["ops"]})
    for i in range(3):
        fail(s, store, f"boom {i}")
        clock.advance(1)
    assert sent_types() == ["run.failed"] * 3
    [row] = store.errors_of("orders")
    assert (row["count"], row["status"], row["step"]) == (3, "open", "load")


def test_dedupe_alerts_once_and_counts_repeats(store, clock):
    s = spec({"on": ["run.alert", "run.failed"], "to": ["ops"], "dedupe": True})
    fail(s, store, "boom 1")
    assert sent_types() == ["run.alert", "run.failed"]  # both events of the first run
    for i in range(5):
        clock.advance(1)
        fail(s, store, f"boom {i + 2}")
    assert sent_types() == ["run.alert", "run.failed"]
    [row] = store.errors_of("orders")
    assert row["count"] == 6 and row["last_seen"] - row["first_seen"] == 5 * 3600
    fail(s, store, "another failure")  # a new fingerprint alerts
    assert sent_types()[-1] == "run.failed"
    assert len(store.errors_of("orders")) == 2


def test_remind_every_alerts_again_with_the_count(store, clock):
    s = spec({"on": "run.failed", "to": ["ops"], "remind_every": "6h"})
    for _ in range(13):  # hourly for 12 hours
        fail(s, store, "boom")
        clock.advance(1)
    texts = [text for _, text, _ in SENT]
    assert len(texts) == 3  # hour 0, 6 and 12
    assert "error_count=1" in texts[0] and "error_count=7" in texts[1] and "error_count=13" in texts[2]


def test_resolved_notice_on_first_success(store, clock):
    s = spec({"on": "run.failed", "to": ["ops"], "dedupe": True})
    fail(s, store, "boom")
    fail(s, store, "boom")
    clock.advance(2)
    ok(s, store)
    ok(s, store)  # nothing open any more: no second notice
    assert sent_types() == ["run.failed", "run.resolved"]
    event = SENT[-1][2]
    assert event["data"]["error_count"] == 2 and event["data"]["errors"] == 1
    [row] = store.errors_of("orders")
    assert row["status"] == "resolved" and row["resolved"] == clock()
    fail(s, store, "boom")  # back again: open, fresh count, alerts again
    assert sent_types()[-1] == "run.failed"
    assert store.errors_of("orders")[0]["count"] == 1


def test_resolved_goes_to_explicit_rules_only_without_dedupe(store, clock):
    s = spec({"on": "run.failed", "to": ["ops"]})
    fail(s, store, "boom")
    ok(s, store)
    assert sent_types() == ["run.failed"]
    SENT.clear()
    s = spec({"on": "run.resolved", "to": ["ops"]})
    fail(s, store, "boom")
    ok(s, store)
    assert sent_types() == ["run.resolved"]


def test_remind_every_turns_dedupe_on_and_is_checked():
    assert spec({"on": "run.failed", "to": ["ops"], "remind_every": "6h"}).notify.rules[0].dedupe
    with pytest.raises(ValueError, match="duration"):
        spec({"on": "run.failed", "to": ["ops"], "remind_every": "often"})


# ---------------------------------------------------------------- digest
def test_errors_section_in_the_digest(store, clock):
    s = spec(digest={"sections": ["errors"]})
    fail(s, store, "boom")
    clock.advance(1)
    fail(s, store, "boom")
    clock.advance(7)  # 08:00
    out = dg.send_due(s, store, clock.dt())
    assert out == {"ops": "sent"}
    text = SENT[-1][1]
    assert "errors: open 1, resolved 0" in text
    assert "orders: RuntimeError: boom" in text and "(step load) x2" in text
    assert "first 2026-01-01T00:00:00Z, last 2026-01-01T01:00:00Z" in text
    assert dg.send_due(s, store, clock.dt()) == {"ops": "already sent"}
    ok(s, store)
    clock.advance(24)
    dg.send_due(s, store, clock.dt())
    assert "errors: open 0, resolved 1" in SENT[-1][1] and "resolved 2026-01-01T08:00:00Z" in SENT[-1][1]
    clock.advance(24)  # resolved more than a period ago: clean, heartbeat
    dg.send_due(s, store, clock.dt())
    assert "errors: none open" in SENT[-1][1] and "all clear: no run errors" in SENT[-1][1]
    assert SENT[-1][2]["data"]["all_clear"]


def test_errors_only_digest_has_its_own_guard(store, clock):
    s = spec(digest={})  # default sections: check findings only
    fail(s, store, "boom")
    clock.advance(8)
    assert "errors" not in dg.show(s, store, clock.dt())
    text = dg.show(s, store, clock.dt(), only_errors=True)
    assert "errors: open 1" in text and "no check step" not in text
    assert dg.send_due(s, store, clock.dt(), only_errors=True) == {"ops": "sent"}
    assert SENT[-1][2]["type"] == "errors.digest.daily"
    assert dg.send_due(s, store, clock.dt(), only_errors=True) == {"ops": "already sent"}
    assert dg.send_due(s, store, clock.dt()) == {"ops": "sent"}  # the check digest is separate


def test_cli_digest_errors(tmp_path, store, clock, capsys):
    from agentl.cli import main

    db = str(store.path) if isinstance(store, Store) and not isinstance(store, PostgresStore) else store.dsn
    fail(spec(digest={}), store, "boom")
    path = tmp_path / "p.yaml"
    path.write_text(
        "name: orders\nsteps:\n  - id: load\n    use: test.errors_src\n"
        "notify:\n  channels:\n    ops: {type: fake_errors}\n  digest: {to: [ops]}\n"
    )
    assert main(["digest", str(path), "--errors", "--show", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "errors: open 1" in out and "x1" in out
