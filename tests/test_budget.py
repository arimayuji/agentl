import pytest

from agentl import SpecError, parse, register_provider
from agentl.llm import Result
from agentl.runner import Runner
from agentl.store import Store


class Metered:
    """Fake provider: every call costs 1000 input + 500 output tokens."""

    calls = 0

    def __init__(self, spec):
        pass

    def structured(self, instruction, record, schema):
        Metered.calls += 1
        return Result(schema.model_validate({"ok": True}), 1000, 500)

    def text(self, instruction, content):
        return ""


register_provider("metered", Metered)


def pipeline(budget, price=None, n=10):
    llm = {"provider": "metered", "model": "m"}
    if price:
        llm["price"] = price
    return parse(
        {
            "name": "b",
            "llm": {"default": llm},
            "budget": budget,
            "steps": [
                {"id": "src", "fn": "tests.test_budget:rows", "with": {"n": n}},
                {"id": "ask", "agent": "Check.", "input": "src", "output": {"ok": "bool"}},
            ],
        }
    )


def rows(input=None, n=10):
    return [{"i": i} for i in range(n)]


@pytest.fixture
def run(tmp_path):
    Metered.calls = 0
    events = []

    def go(spec):
        with Store(tmp_path / "db.sqlite") as store:
            return Runner(spec, store=store, use_cache=False, listeners=[events.append]).run(), events

    return go


def test_token_budget_stops_the_run(run):
    result, events = run(pipeline({"max_tokens": 4000}))
    assert result.status == "failed"
    assert "BudgetExceeded" in result.error
    assert Metered.calls == 3  # 1500 tokens each: stops once 4000 is reached
    exceeded = [e for e in events if e["type"] == "budget.exceeded"]
    assert exceeded and exceeded[0]["step"] == "ask"
    assert exceeded[0]["data"]["input_tokens"] == 3000


def test_usd_budget_and_cost_reporting(run):
    price = {"input": 3.0, "output": 15.0}  # per million tokens -> 0.0105 USD per call
    result, events = run(pipeline({"max_usd": 0.05}, price=price))
    assert result.status == "failed"
    assert Metered.calls == 5
    assert result.cost_usd == pytest.approx(0.0525)

    result, events = run(pipeline({"max_usd": 1.0}, price=price, n=2))
    assert result.status == "succeeded"
    done = [e for e in events if e["type"] == "run.succeeded"][-1]
    assert done["data"]["cost_usd"] == pytest.approx(0.021)


def test_usd_budget_requires_a_price():
    with pytest.raises(SpecError, match="price"):
        pipeline({"max_usd": 1.0})


def test_spend_per_step_in_events_and_dashboard(tmp_path):
    from agentl.dashboard import run_detail
    from agentl.store import Store

    spec = parse(
        {
            "name": "two",
            "llm": {
                "default": {"provider": "metered", "model": "m", "price": {"input": 3.0, "output": 15.0}}
            },
            "steps": [
                {"id": "src", "fn": "tests.test_budget:rows", "with": {"n": 2}},
                {"id": "a", "agent": "A.", "input": "src", "output": {"ok": "bool"}},
                {"id": "b", "agent": "B.", "input": "a", "output": {"ok": "bool"}},
                {"id": "c", "use": "select", "input": "b"},
            ],
        }
    )
    with Store(tmp_path / "db.sqlite") as store:
        result = Runner(spec, store=store, use_cache=False).run()
        detail = run_detail(store, result.run_id)
    models = result.spend["a"].pop("models")
    assert list(models) == ["m"] and models["m"]["llm_calls"] == 2
    assert models["m"]["cost_usd"] == pytest.approx(0.021)
    assert result.spend["a"] == {
        "input_tokens": 2000,
        "output_tokens": 1000,
        "cache_read_tokens": 0,
        "llm_calls": 2,
        "tool_calls": 0,
        "cost_usd": 0.021,
        "answered_by": {"default": 2},
        "model": "m",
        "cache_hits": 0,
    }
    assert set(result.spend) == {"a", "b"}  # only agent steps
    assert detail["spend"]["steps"]["b"]["cost_usd"] == pytest.approx(0.021)
    assert detail["spend"]["total"] == {"input_tokens": 4000, "output_tokens": 2000, "cost_usd": 0.042}
    assert result.summary()["spend"]["a"]["input_tokens"] == 2000


class Grouped(Metered):
    """Answers a whole group (`records_per_call`) in one call."""

    def structured(self, instruction, record, schema):
        Metered.calls += 1
        items = [{"index": i, "ok": True} for i in range(len(record["records"]))]
        return Result(schema.model_validate({"items": items}), 1000, 500)


register_provider("grouped", Grouped)


def test_budget_ticket_is_released_from_another_thread(tmp_path):
    """The charge may run on another thread than the check: the ticket, not the thread,
    releases the reservation."""
    import threading

    from agentl.runner import BudgetExceeded, RunResult

    spec = pipeline({"max_usd": 0.05}, price={"input": 3.0, "output": 15.0})
    with Store(tmp_path / "db.sqlite") as store:
        runner = Runner(spec, store=store, use_cache=False)
        result = RunResult(run_id="r", status="running")
        result.spend["ask"] = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
        result.spend["ask"] |= {"llm_calls": 0, "cost_usd": 0.0}
        usage = Result(None, 1000, 500)
        for _ in range(4):
            ticket = runner._check_budget(result, "ask")
            args = (result, spec.steps[1], spec.llm["default"], usage)
            t = threading.Thread(target=runner._charge, args=args, kwargs={"ticket": ticket})
            t.start()
            t.join()
        assert runner._inflight == {}  # 4 calls charged elsewhere, nothing leaked
        runner._check_budget(result, "ask")  # 0.042 spent: the 5th call is allowed
        with pytest.raises(BudgetExceeded, match="max_usd"):
            runner._check_budget(result, "ask")  # 0.042 + one in flight >= 0.05


def test_grouped_parallel_calls_do_not_leak_and_stop_at_the_limit(tmp_path):
    def go(budget, n):
        spec = pipeline(budget, price={"input": 3.0, "output": 15.0}, n=n)  # 0.0105 USD a call
        spec.llm["default"].provider = "grouped"
        spec.steps[1].records_per_call = 2
        spec.steps[1].concurrency = 4
        Metered.calls = 0
        with Store(tmp_path / f"{n}.sqlite") as store:
            runner = Runner(spec, store=store, use_cache=False)
            return runner.run(), runner

    result, runner = go({"max_usd": 1.0}, 10)
    assert result.status == "succeeded" and Metered.calls == 5  # not stopped early
    assert runner._inflight == {}
    result, runner = go({"max_usd": 0.03}, 20)
    assert result.status == "failed" and Metered.calls == 3
    assert runner._inflight == {}
