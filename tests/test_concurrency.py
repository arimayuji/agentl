import os
import threading
import time
import uuid

import pytest

from agentl import parse
from agentl.cli import main
from agentl.connectors.transforms import cache_step
from agentl.registry import connector
from agentl.runner import Runner, run
from agentl.store import PostgresStore, Store, open_store

DSN = os.environ.get("AGENTL_TEST_PG_DSN")
CALLS: list[dict] = []


@connector("test.slow_source")
def slow_source(input=None, value="x", delay=0.2):
    CALLS.append({"value": value})
    time.sleep(delay)
    return [{"value": value}]


@pytest.fixture(params=["sqlite", "postgres"])
def target(request, tmp_path):
    if request.param == "postgres":
        if not DSN:
            pytest.skip("set AGENTL_TEST_PG_DSN to run Postgres tests")
        return DSN
    return str(tmp_path / "s.db")


@pytest.fixture
def store(target):
    s = open_store(target)
    assert isinstance(s, PostgresStore) if target == DSN else type(s) is Store
    yield s
    s.close()


def test_cache_ttl_expires(store, monkeypatch):
    k = f"ttl:{uuid.uuid4().hex}"
    store.cache_set(k, [1], ttl=60)
    store.cache_set(k + ":forever", [2])
    assert store.cache_get(k) == [1]
    now = time.time()
    monkeypatch.setattr("agentl.store.time.time", lambda: now + 61)  # no sleep: slow runners stay green
    assert store.cache_get(k) is None
    assert store.cache_get(k + ":forever") == [2]


def test_cache_clear_key_and_prefix(store):
    p = f"clr{uuid.uuid4().hex[:6]}"
    for k in (f"{p}:a", f"{p}:b", f"{p}_x"):
        store.cache_set(k, 1)
    assert store.cache_clear(f"{p}:a") == 1
    assert store.cache_clear(f"{p}:*") == 1  # `_` in the prefix is literal, not a wildcard
    assert store.cache_get(f"{p}_x") == 1


def test_lease_is_exclusive_and_expires(store):
    k = f"lock:{uuid.uuid4().hex}"
    assert store.lock_acquire(k, "a", 0.2)
    assert not store.lock_acquire(k, "b", 0.2)
    assert store.lock_acquire(k, "a", 0.2)  # the holder extends it
    time.sleep(0.3)  # holder died: the lease lapses
    assert store.lock_acquire(k, "b", 10)
    store.lock_release(k, "a")  # not the holder: no effect
    assert store.lock_holder(k)["owner"] == "b"
    store.lock_release(k, "b")
    assert store.lock_holder(k) is None


def test_lease_is_renewed_while_held(store):
    k = f"lock:{uuid.uuid4().hex}"
    with store.lease(k, 0.3) as held:
        assert held
        time.sleep(0.7)  # longer than the ttl: the heartbeat keeps it
        assert not store.lock_acquire(k, "other", 0.3)
    assert store.lock_acquire(k, "other", 0.3)


def test_single_flight_threads_and_stores(target):
    """Threads on one store and on separate connections (as separate workers) compute once."""
    k = f"sf:{uuid.uuid4().hex}"
    calls = []
    stores = [open_store(target) for _ in range(3)]

    def compute():
        calls.append(1)
        time.sleep(0.3)
        return [{"n": 42}]

    out = []
    threads = [
        threading.Thread(target=lambda s=s: out.append(s.cache_compute(k, compute, ttl=60)))
        for s in stores * 2
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for s in stores:
        s.close()
    assert len(calls) == 1
    assert out == [[{"n": 42}]] * 6


def test_single_flight_takes_over_from_dead_holder(store):
    k = f"sf:{uuid.uuid4().hex}"
    store.lock_acquire("cache:" + k, "dead-worker", 0.2)  # never released
    assert store.cache_compute(k, lambda: [1], lease=1) == [1]


def test_cache_step_in_pipeline(target):
    CALLS.clear()
    key = f"catalog:{uuid.uuid4().hex[:6]}"
    spec = parse(
        {
            "name": "catalog",
            "steps": [
                {
                    "id": "catalog",
                    "use": "cache",
                    "with": {
                        "key": key,
                        "ttl": "1d",
                        "do": [{"use": "test.slow_source", "with": {"value": "a", "delay": 0}}],
                    },
                }
            ],
        }
    )
    for _ in range(2):
        r = run(spec, db=target)
        assert r.status == "succeeded" and r.outputs["catalog"] == [{"value": "a"}]
    assert len(CALLS) == 1
    assert main(["cache", "clear", key, "--db", target]) == 0
    run(spec, db=target)
    assert len(CALLS) == 2


def test_cache_step_validates_do(store):
    with pytest.raises(ValueError, match="do"):
        cache_step(store, None, do=[{"with": {}}])


def _spec(name, on_overlap="skip", wait="1s"):
    return parse(
        {
            "name": name,
            "concurrency": {"on_overlap": on_overlap, "wait": wait, "lease": "10s"},
            "steps": [{"id": "a", "use": "test.slow_source", "with": {"delay": 0}}],
        }
    )


def test_overlap_skip_is_not_a_failure(store):
    name = f"p{uuid.uuid4().hex[:6]}"
    store.lock_acquire("run:" + name, "other-run", 10)
    r = run(_spec(name), store=store, raise_on_failure=True)
    assert r.status == "skipped" and "other-run" in r.error
    assert store.run(r.run_id)["status"] == "skipped"
    assert [e["type"] for e in store.events(r.run_id)] == ["run.skipped"]
    store.lock_release("run:" + name, "other-run")
    assert run(_spec(name), store=store).status == "succeeded"


def test_overlap_fail_and_queue(store):
    name = f"p{uuid.uuid4().hex[:6]}"
    store.lock_acquire("run:" + name, "other-run", 10)
    assert run(_spec(name, "fail"), store=store).status == "failed"
    assert run(_spec(name, "queue", wait="0.3s"), store=store).status == "failed"
    threading.Timer(0.3, lambda: store.lock_release("run:" + name, "other-run")).start()
    assert run(_spec(name, "queue", wait="5s"), store=store).status == "succeeded"


def test_concurrent_runs_one_at_a_time(target):
    name = f"p{uuid.uuid4().hex[:6]}"
    spec = parse(
        {
            "name": name,
            "concurrency": {"key": "{pipeline}:daily"},
            "steps": [{"id": "a", "use": "test.slow_source", "with": {"delay": 2}}],
        }
    )
    statuses = []
    ready = threading.Barrier(3, timeout=30)  # start the three runs together, after each opened its store
    with open_store(target):
        pass  # create the schema once, so the threads do not race on it

    def go():
        try:
            with open_store(target) as s:
                runner = Runner(spec, store=s)
                ready.wait()
                statuses.append(runner.run().status)
        except BaseException:
            ready.abort()  # never leave the other threads waiting on the barrier
            raise

    threads = [threading.Thread(target=go) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(statuses) == ["skipped", "skipped", "succeeded"]
