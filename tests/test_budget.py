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
    assert result.spend["a"] == {
        "input_tokens": 2000,
        "output_tokens": 1000,
        "cache_read_tokens": 0,
        "llm_calls": 2,
        "cost_usd": 0.021,
    }
    assert set(result.spend) == {"a", "b"}  # only agent steps
    assert detail["spend"]["steps"]["b"]["cost_usd"] == pytest.approx(0.021)
    assert detail["spend"]["total"] == {"input_tokens": 4000, "output_tokens": 2000, "cost_usd": 0.042}
    assert result.summary()["spend"]["a"]["input_tokens"] == 2000
