"""Best-effort steps and order-only dependencies (#206)."""

import pytest

from agentl import parse
from agentl.graph import mermaid, order
from agentl.runner import Runner
from agentl.store import Store

CALLS: list[str] = []


def source(input=None):
    return [{"id": i} for i in range(3)]


def sink(input, name="x", fail=False):
    CALLS.append(name)
    if fail:
        raise RuntimeError(f"{name} is down")
    return [{"written": len(input)}]


def _spec(legacy: dict, new: dict | None = None, extra=()):
    fn = "tests.test_criticality:"
    return parse(
        {
            "name": "migrate",
            "steps": [
                {"id": "rows", "fn": fn + "source"},
                {"id": "new", "fn": fn + "sink", "input": "rows", "with": {"name": "new"}, **(new or {})},
                {"id": "legacy", "fn": fn + "sink", "input": "rows", **legacy},
                *extra,
            ],
        }
    )


def _run(tmp_path, spec):
    with Store(tmp_path / "a.db") as store:
        result = Runner(spec, store=store).run()
        return result, store.events(result.run_id)


@pytest.fixture(autouse=True)
def reset():
    CALLS.clear()


def test_best_effort_failure_does_not_fail_the_run(tmp_path):
    spec = _spec({"required": False, "with": {"name": "legacy", "fail": True}})
    result, events = _run(tmp_path, spec)
    assert result.status == "succeeded"
    assert result.partial
    assert "legacy is down" in result.best_effort_failed["legacy"]
    assert result.outputs["new"] == [{"written": 3}]
    failed = [e for e in events if e["type"] == "step.failed"]
    assert failed[0]["step"] == "legacy" and failed[0]["data"]["required"] is False
    done = next(e for e in events if e["type"] == "run.succeeded")
    assert done["data"]["partial"] is True and done["data"]["best_effort_failed"] == ["legacy"]
    summary = result.summary()
    assert summary["partial"] is True and "legacy" in summary["best_effort_failed"]


def test_required_failure_still_fails(tmp_path):
    spec = _spec({"with": {"name": "legacy", "fail": True}})
    result, _ = _run(tmp_path, spec)
    assert result.status == "failed" and not result.partial


def test_clean_run_is_not_partial(tmp_path):
    result, _ = _run(tmp_path, _spec({"criticality": "best_effort", "with": {"name": "legacy"}}))
    assert result.status == "succeeded" and not result.partial
    assert result.summary()["partial"] is False


def test_after_orders_writers(tmp_path):
    # legacy runs only after new, even though both read rows and could run in parallel
    spec = _spec({"required": False, "after": "new", "with": {"name": "legacy"}})
    result, _ = _run(tmp_path, spec)
    assert result.status == "succeeded"
    assert CALLS == ["new", "legacy"]
    assert "new -.-> legacy" in mermaid(spec)


def test_after_a_failed_best_effort_step_still_runs(tmp_path):
    spec = _spec(
        {"required": False, "with": {"name": "legacy", "fail": True}},
        new={"after": ["legacy"]},
    )
    result, _ = _run(tmp_path, spec)
    assert result.status == "succeeded" and result.partial
    assert CALLS == ["legacy", "new"]


def test_spec_rules():
    with pytest.raises(ValueError, match="best effort"):
        _spec(
            {"required": False, "with": {"name": "legacy"}},
            extra=[{"id": "next", "fn": "tests.test_criticality:sink", "input": "legacy"}],
        )
    with pytest.raises(ValueError, match="criticality"):
        _spec({"criticality": "sometimes"})
    with pytest.raises(ValueError, match="either required or criticality"):
        _spec({"criticality": "best_effort", "required": True})
    with pytest.raises(ValueError, match="unknown step"):
        _spec({"after": ["nope"]})
    with pytest.raises(ValueError, match="cycle"):
        order(_spec({"after": ["new"]}, new={"after": ["legacy"]}))
