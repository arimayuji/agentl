"""Independent steps run in parallel (#57)."""

import threading
import time

import pytest

from agentl import parse
from agentl.llm import Result, register_provider
from agentl.runner import Runner
from agentl.store import Store

STARTED: list[str] = []
LOCK = threading.Lock()
# How many `slow` steps are running right now, and the most seen at once. Asserting on this instead
# of wall time keeps the tests stable on slow CI runners.
ACTIVE = {"now": 0, "max": 0}


def slow(input=None, name="x", seconds=0.3, fail=False):
    with LOCK:
        STARTED.append(name)
        ACTIVE["now"] += 1
        ACTIVE["max"] = max(ACTIVE["max"], ACTIVE["now"])
    try:
        time.sleep(seconds)
    finally:
        with LOCK:
            ACTIVE["now"] -= 1
    if fail:
        raise RuntimeError(f"{name} broke")
    return [{"src": name, "n": i} for i in range(3)]


def join(input):
    return [r for rows in input.values() for r in rows]


def pipeline(parallel=4, fail_a=False, extra=()):
    return parse(
        {
            "name": "par",
            "max_parallel_steps": parallel,
            "steps": [
                {"id": "a", "fn": "tests.test_parallel_steps:slow", "with": {"name": "a", "fail": fail_a}},
                {"id": "b", "fn": "tests.test_parallel_steps:slow", "with": {"name": "b"}},
                {"id": "both", "fn": "tests.test_parallel_steps:join", "input": ["a", "b"]},
                *extra,
            ],
        }
    )


@pytest.fixture(autouse=True)
def reset():
    STARTED.clear()
    ACTIVE.update(now=0, max=0)


def run(tmp_path, spec):
    with Store(tmp_path / "db") as store:
        t0 = time.time()
        r = Runner(spec, store=store).run()
        return r, time.time() - t0, store.events(r.run_id)


def test_independent_sources_run_together(tmp_path):
    r, _, _ = run(tmp_path, pipeline(parallel=4))
    assert r.status == "succeeded", r.error
    assert ACTIVE["max"] == 2  # a and b overlapped
    assert len(r.outputs["both"]) == 6


def test_one_at_a_time_keeps_the_old_order(tmp_path):
    _, _, events = run(tmp_path, pipeline(parallel=1))
    assert ACTIVE["max"] == 1
    order = [(e["type"], e["step"]) for e in events if e["type"].startswith("step.")]
    assert order == [
        ("step.started", "a"),
        ("step.succeeded", "a"),
        ("step.started", "b"),
        ("step.succeeded", "b"),
        ("step.started", "both"),
        ("step.succeeded", "both"),
    ]


def test_same_outputs_parallel_and_sequential(tmp_path):
    seq, _, _ = run(tmp_path / "s", pipeline(parallel=1))
    par, _, _ = run(tmp_path / "p", pipeline(parallel=4))
    assert seq.outputs == par.outputs


def test_failure_stops_new_steps_and_lets_running_ones_finish(tmp_path):
    late = {"id": "c", "fn": "tests.test_parallel_steps:slow", "with": {"name": "c"}, "input": "a"}
    r, _, events = run(tmp_path, pipeline(parallel=4, fail_a=True, extra=[late]))
    assert r.status == "failed" and "a broke" in r.error
    assert "b" in r.outputs  # was already running, finished
    assert "both" not in r.outputs and "c" not in STARTED
    failed = [e["step"] for e in events if e["type"] == "step.failed"]
    assert failed == ["a"]


def meet(input=None, name="x"):
    """Waits (up to 5 s) until another step is running too, so the overlap does not depend on how
    fast a CI machine starts threads; then holds a little longer."""
    with LOCK:
        ACTIVE["now"] += 1
        ACTIVE["max"] = max(ACTIVE["max"], ACTIVE["now"])
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with LOCK:
                if ACTIVE["max"] >= 2:
                    break
            time.sleep(0.01)
        time.sleep(0.05)
    finally:
        with LOCK:
            ACTIVE["now"] -= 1
    return [{"src": name}]


def test_parallel_limit_is_respected(tmp_path):
    many = [
        {"id": f"s{i}", "fn": "tests.test_parallel_steps:meet", "with": {"name": f"s{i}"}} for i in range(4)
    ]
    spec = parse({"name": "lim", "max_parallel_steps": 2, "steps": many})
    r, _, _ = run(tmp_path, spec)
    assert r.status == "succeeded", r.error
    assert ACTIVE["max"] == 2  # never more than the limit, and it did run two at once


class Priced:
    def __init__(self, spec):
        self.tokens = int(spec.model)

    def structured(self, instruction, record, schema):
        time.sleep(0.01)
        return Result(schema.model_validate({"ok": True}), self.tokens, 0)

    def text(self, instruction, content):
        return ""


register_provider("priced", Priced)


def test_spend_per_step_is_right_when_agent_steps_overlap(tmp_path):
    spec = parse(
        {
            "name": "spend",
            "llm": {
                "small": {"provider": "priced", "model": "10", "price": {"input": 1, "output": 1}},
                "big": {"provider": "priced", "model": "1000", "price": {"input": 1, "output": 1}},
            },
            "steps": [
                {"id": "src", "fn": "tests.test_parallel_steps:slow", "with": {"seconds": 0}},
                {"id": "x", "agent": "X", "input": "src", "llm": "small", "output": {"ok": "bool"}},
                {"id": "y", "agent": "Y", "input": "src", "llm": "big", "output": {"ok": "bool"}},
            ],
        }
    )
    r, _, _ = run(tmp_path, spec)
    assert r.status == "succeeded", r.error
    assert r.spend["x"]["input_tokens"] == 30 and r.spend["y"]["input_tokens"] == 3000
    assert r.input_tokens == 3030 and r.llm_calls == 6
