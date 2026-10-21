"""Isolated series and SQL conflict retries (#206)."""

import sqlite3

import pytest

from agentl import parse
from agentl import runner as runner_mod
from agentl.runner import Runner, default_retries, sql_conflict
from agentl.spec import SpecError
from agentl.store import Store

CALLS: list[list[str]] = []


def source(input=None):
    return [{"series": s, "v": i} for i, s in enumerate(["a", "b", "a", "c", "b"])]


def sink(input, fail=()):
    names = sorted({r["series"] for r in input})
    CALLS.append(names)
    if any(n in fail for n in names):
        raise RuntimeError(f"cannot write {names}")
    return [{"written": len(input)}]


def _spec(save: dict, extra=()):
    fn = "tests.test_isolate:"
    return parse(
        {
            "name": "isolated",
            "steps": [
                {"id": "rows", "fn": fn + "source"},
                {"id": "save", "fn": fn + "sink", "input": "rows", **save},
                *extra,
            ],
        }
    )


def _run(tmp_path, spec):
    with Store(tmp_path / "a.db") as store:
        result = Runner(spec, store=store).run()
        return result, store.events(result.run_id)


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    CALLS.clear()
    waits: list[float] = []
    monkeypatch.setattr(runner_mod, "_sleep", waits.append)
    monkeypatch.setattr(runner_mod, "_jitter", lambda: 1.0)
    return waits


def test_failed_series_does_not_stop_the_others(tmp_path):
    spec = _spec({"isolate": "per_series", "series": "series", "with": {"fail": ["b"]}})
    result, events = _run(tmp_path, spec)
    assert CALLS == [["a"], ["b"], ["c"]]
    assert result.status == "succeeded" and result.outcome == "partial" and result.partial
    assert list(result.series_failed["save"]) == ["b"]
    assert "cannot write" in result.series_failed["save"]["b"]
    assert result.outputs["save"] == [{"written": 2}, {"written": 1}]
    failed = [e for e in events if e["type"] == "step.series_failed"]
    assert [(e["step"], e["data"]["series"], e["data"]["rows"]) for e in failed] == [("save", "b", 2)]
    iso = next(e for e in events if e["type"] == "step.isolated")
    assert iso["data"] == {"series": 3, "failed": 1}
    done = next(e for e in events if e["type"] == "run.succeeded")
    assert done["data"]["series_failed"] == {"save": ["b"]}
    assert result.summary()["series_failed"] == {"save": {"b": result.series_failed["save"]["b"]}}


def test_composite_series_and_all_ok(tmp_path):
    spec = _spec({"isolate": "per_series", "series": ["series", "v"]})
    result, _ = _run(tmp_path, spec)
    assert result.outcome == "success" and not result.series_failed
    assert len(CALLS) == 5


def test_every_series_failed_fails_the_step(tmp_path):
    spec = _spec({"isolate": "per_series", "series": "series", "with": {"fail": ["a", "b", "c"]}})
    result, _ = _run(tmp_path, spec)
    assert result.status == "failed" and "cannot write ['a']" in result.error


def test_every_series_failed_on_best_effort_step(tmp_path):
    spec = _spec(
        {"isolate": "per_series", "series": "series", "required": False, "with": {"fail": ["a", "b", "c"]}}
    )
    result, _ = _run(tmp_path, spec)
    assert result.status == "succeeded" and "save" in result.best_effort_failed


TRIES: dict[str, int] = {}


def flaky(input):
    name = input[0]["series"]
    TRIES[name] = TRIES.get(name, 0) + 1
    if name == "b" and TRIES[name] == 1:
        raise TimeoutError("slow")
    return [{"written": len(input)}]


def test_each_series_is_retried_on_its_own(tmp_path, setup):
    TRIES.clear()
    spec = _spec(
        {"fn": "tests.test_isolate:flaky", "isolate": "per_series", "series": "series", "retries": {}}
    )
    result, _ = _run(tmp_path, spec)
    assert result.outcome == "success" and TRIES == {"a": 1, "b": 2, "c": 1}
    assert setup == [1.0]


@pytest.mark.parametrize(
    "save, msg",
    [
        ({"isolate": "per_series"}, "needs `series`"),
        ({"series": "series"}, "series needs isolate"),
        ({"isolate": "per_key"}, "isolate"),
    ],
)
def test_isolate_validation(save, msg):
    with pytest.raises((SpecError, ValueError), match=msg):
        _spec(save)


def test_isolate_needs_one_input():
    with pytest.raises((SpecError, ValueError), match="one input"):
        parse(
            {
                "name": "x",
                "steps": [
                    {"id": "a", "fn": "tests.test_isolate:source"},
                    {"id": "b", "fn": "tests.test_isolate:source"},
                    {
                        "id": "c",
                        "fn": "tests.test_isolate:sink",
                        "input": ["a", "b"],
                        "isolate": "per_series",
                        "series": "series",
                    },
                ],
            }
        )


# SQL conflicts --------------------------------------------------------------


def test_sql_conflict_classifies_sqlite_errors(tmp_path):
    db = tmp_path / "x.db"
    with sqlite3.connect(db) as conn:
        conn.execute("create table t (id integer primary key)")
        conn.execute("insert into t values (1)")
        with pytest.raises(sqlite3.IntegrityError) as dup:
            conn.execute("insert into t values (1)")
    assert sql_conflict(dup.value) == "unique_violation"
    locker = sqlite3.connect(db)
    locker.execute("begin exclusive")
    with pytest.raises(sqlite3.OperationalError) as locked:
        sqlite3.connect(db, timeout=0).execute("insert into t values (2)")
    locker.rollback()
    locker.close()
    assert sql_conflict(locked.value) == "deadlock"
    assert sql_conflict(RuntimeError("x")) is None
    wrapped = RuntimeError("write failed")
    wrapped.__cause__ = dup.value
    assert sql_conflict(wrapped) == "unique_violation"


def test_default_retries_of_sql_writers():
    def step(use, **with_):
        return parse(
            {
                "name": "x",
                "steps": [
                    {"id": "a", "fn": "tests.test_isolate:source"},
                    {"id": "w", "use": use, "input": "a", "with": with_},
                ],
            }
        ).step("w")

    assert default_retries(step("sqlite.write", db="x.db", table="t")).on == ["deadlock"]
    r = default_retries(step("postgres.write", dsn="x", table="t", mode="upsert", key="id"))
    assert r is not None and r.attempts == 3 and r.on == ["deadlock", "unique_violation"]
    assert default_retries(step("csv.write", path="x.csv")) is None


def test_sqlite_write_retries_a_locked_database(tmp_path, monkeypatch):
    db = tmp_path / "out.db"
    with sqlite3.connect(db) as conn:
        conn.execute("create table facts (series text, v integer)")
    real = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: real(*a, **{**k, "timeout": 0}))
    locker = real(db, check_same_thread=False)
    locker.execute("begin exclusive")
    waits: list[float] = []

    def release(seconds):
        waits.append(seconds)
        locker.rollback()

    monkeypatch.setattr(runner_mod, "_sleep", release)
    spec = parse(
        {
            "name": "locked",
            "steps": [
                {"id": "rows", "fn": "tests.test_isolate:source"},
                {
                    "id": "save",
                    "use": "sqlite.write",
                    "input": "rows",
                    "with": {"db": str(db), "table": "facts"},
                },
            ],
        }
    )
    result, events = _run(tmp_path, spec)
    locker.close()
    assert result.status == "succeeded", result.error
    assert waits == [0.2]
    retry = next(e for e in events if e["type"] == "step.retry")
    assert "locked" in retry["data"]["error"]
    with real(db) as conn:
        assert conn.execute("select count(*) from facts").fetchone()[0] == 5


def test_explicit_retries_turn_the_default_off(tmp_path, monkeypatch):
    db = tmp_path / "out.db"
    with sqlite3.connect(db) as conn:
        conn.execute("create table facts (series text, v integer)")
    real = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: real(*a, **{**k, "timeout": 0}))
    locker = real(db)
    locker.execute("begin exclusive")
    spec = parse(
        {
            "name": "locked",
            "steps": [
                {"id": "rows", "fn": "tests.test_isolate:source"},
                {
                    "id": "save",
                    "use": "sqlite.write",
                    "input": "rows",
                    "retries": {"attempts": 1},
                    "with": {"db": str(db), "table": "facts"},
                },
            ],
        }
    )
    result, _ = _run(tmp_path, spec)
    locker.rollback()
    locker.close()
    assert result.status == "failed" and "locked" in result.error
