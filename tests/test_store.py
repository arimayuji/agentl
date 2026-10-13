import multiprocessing
import os
import uuid

import pytest

from agentl import parse, register_provider
from agentl.llm import Result
from agentl.runner import Runner
from agentl.store import PostgresStore, Store, open_store

DSN = os.environ.get("AGENTL_TEST_PG_DSN")


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "postgres":
        if not DSN:
            pytest.skip("set AGENTL_TEST_PG_DSN to run Postgres tests")
        s = open_store(DSN)
        assert isinstance(s, PostgresStore)
    else:
        s = open_store(tmp_path / "s.db")
        assert type(s) is Store
    yield s
    s.close()


def test_store_contract(store):
    rid = uuid.uuid4().hex
    store.start_run(rid, "p", "flowchart LR")
    store.event(rid, "step.started", "a", {"kind": "use", "n": 1})
    store.event(rid, "run.succeeded", None, {"seconds": 0.1})
    store.finish_run(rid, "succeeded")
    run = store.run(rid)
    assert run["status"] == "succeeded" and run["diagram"] == "flowchart LR"
    assert [e["type"] for e in store.events(rid)] == ["step.started", "run.succeeded"]
    assert store.events(rid)[0]["data"] == {"kind": "use", "n": 1}
    assert any(r["id"] == rid for r in store.runs(limit=50))

    key = f"k-{rid}"
    assert store.cache_get(key) is None
    store.cache_set(key, {"a": 1})
    store.cache_set(key, {"a": 2})  # upsert
    assert store.cache_get(key) == {"a": 2}


def test_open_store_uses_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTL_DB", str(tmp_path / "env.db"))
    with open_store() as s:
        assert s.path == tmp_path / "env.db"


class Counting:
    def __init__(self, spec):
        pass

    def structured(self, instruction, record, schema):
        return Result(schema.model_validate({"tag": f"t{record['i']}"}), 1, 1)

    def text(self, instruction, content):
        return ""


def _worker(dsn: str, n: int, queue) -> None:
    register_provider("counting", Counting)
    spec = parse(
        {
            "name": "shared",
            "llm": {"provider": "counting", "model": "m"},
            "steps": [
                {"id": "src", "fn": "tests.test_store:rows", "with": {"n": n}},
                {"id": "tag", "agent": "Tag.", "input": "src", "output": {"tag": "str"}},
            ],
        }
    )
    with open_store(dsn) as store:
        result = Runner(spec, store=store).run()
    queue.put((result.status, result.input_tokens))


def rows(input=None, n=4):
    return [{"i": i, "salt": os.environ.get("AGENTL_TEST_SALT", "")} for i in range(n)]


@pytest.mark.skipif(not DSN, reason="set AGENTL_TEST_PG_DSN to run Postgres tests")
def test_workers_share_history_and_cache(monkeypatch):
    monkeypatch.setenv("AGENTL_TEST_SALT", uuid.uuid4().hex)  # fresh cache keys for this test
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    first = [ctx.Process(target=_worker, args=(DSN, 4, queue)) for _ in range(2)]
    for p in first:
        p.start()
    for p in first:
        p.join(60)
    results = [queue.get(timeout=5) for _ in first]
    assert all(status == "succeeded" for status, _ in results)

    # A third worker finds every record in the shared cache and spends nothing.
    p = ctx.Process(target=_worker, args=(DSN, 4, queue))
    p.start()
    p.join(60)
    assert queue.get(timeout=5) == ("succeeded", 0)

    with open_store(DSN) as store:
        assert sum(r["pipeline"] == "shared" for r in store.runs(limit=500)) >= 3
