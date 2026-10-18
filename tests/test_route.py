"""Router step (#299, part 2): one label per record, each target reads only its branch."""

import pytest
import yaml

from agentl import lint, parse, register_provider
from agentl.dashboard import parse_graph
from agentl.graph import mermaid
from agentl.llm import Result
from agentl.runner import Runner
from agentl.spec import BudgetSpec, SpecError
from agentl.store import Store

CALLS: list[dict] = []  # records the llm was asked about


class Fake:
    """Labels a record from its `hint` field; `hint: weird` answers a label not in the routes."""

    def __init__(self, spec):
        self.name = spec.model

    def structured(self, instruction, record, schema):
        CALLS.append(record)
        return Result(schema.model_validate({"route": record.get("hint", "other")}), 20, 2)

    def text(self, instruction, content):
        return ""


register_provider("route-fake", Fake)

DOCS = [
    {"id": 1, "type": "nfe", "hint": "contract"},  # the rule wins: no llm call
    {"id": 2, "type": "pdf", "hint": "invoice"},
    {"id": 3, "type": "pdf", "hint": "contract"},
    {"id": 4, "type": "pdf", "hint": "other"},
    {"id": 5, "type": "pdf", "hint": "weird"},
]


def docs(input=None):
    return [dict(d) for d in DOCS]


def tag(records, label="x"):
    return [{**r, "seen_by": label} for r in records]


def steps(**router):
    return [
        {"id": "docs", "fn": "tests.test_route:docs"},
        {
            "id": "route",
            "input": "docs",
            "agent": "Which kind of document is this?",
            "routes": {"invoice": "extract_invoice", "contract": "extract_contract", "other": "drop"},
            "rules": [{"when": "type == 'nfe'", "route": "invoice"}],
            **router,
        },
        {"id": "extract_invoice", "fn": "tests.test_route:tag", "with": {"label": "invoice"}},
        {"id": "extract_contract", "input": "route", "fn": "tests.test_route:tag", "with": {"label": "c"}},
    ]


def pipeline(extra_steps=(), **router):
    return parse(
        {
            "name": "router",
            "llm": {"default": {"provider": "route-fake", "model": "m", "price": {"input": 1, "output": 1}}},
            "steps": [*steps(**router), *extra_steps],
        }
    )


@pytest.fixture(autouse=True)
def _reset():
    CALLS.clear()


def test_routes_records_to_their_branch(tmp_path):
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline(), store=store).run()
        events = [e["data"] for e in store.events(r.run_id) if e["type"] == "route.decided"]
    assert r.status in ("succeeded", "partial")
    assert sorted(c["id"] for c in CALLS) == [2, 3, 4, 5, 5]  # 1: the rule; 5: invalid, retried
    assert [x["id"] for x in r.outputs["extract_invoice"]] == [1, 2]
    assert [x["id"] for x in r.outputs["extract_contract"]] == [3]
    assert [x["id"] for x in r.outputs["route"]] == [1, 2, 3]  # dropped and rejected left out
    assert "route" not in r.outputs["route"][0] and "_route" not in r.outputs["route"][0]
    assert [x["id"] for x in r.rejected["route"]] == [5]
    assert "_error" in r.rejected["route"][0]
    assert r.spend["route"]["routes"] == {"invoice": 2, "contract": 1, "other": 1}
    assert r.spend["route"]["paths"] == {"rules": 1, "agent": 4}
    assert len(events) == 1
    ev = events[0]
    assert ev["counts"] == {"invoice": 2, "contract": 1, "other": 1}
    assert ev["dropped"] == 1 and ev["rejected"] == 1 and ev["rules"] == 1 and ev["total"] == 5
    assert "hint" not in str(ev)  # counts only, no record payloads


def test_cache_hit_on_second_run(tmp_path):
    with Store(tmp_path / "s.db") as store:
        Runner(pipeline(), store=store).run()
        first = len(CALLS)
        r = Runner(pipeline(), store=store).run()
        ev = next(e["data"] for e in store.events(r.run_id) if e["type"] == "route.decided")
    assert first == 5  # the invalid label is asked twice (retry), then rejected
    assert len(CALLS) == first + 2  # only the rejected record is asked again
    assert ev["cached"] == 3 and [x["id"] for x in r.outputs["extract_invoice"]] == [1, 2]


def test_route_tag_and_rules_only(tmp_path):
    rules = [{"when": "type == 'nfe'", "route": "invoice"}, {"when": "id > 1", "route": "contract"}]
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline(rules=rules, route_tag=True), store=store).run()
    assert CALLS == []  # every record matched a rule
    assert [(x["id"], x["_route"]) for x in r.outputs["extract_invoice"]] == [(1, "invoice")]
    assert [x["id"] for x in r.outputs["extract_contract"]] == [2, 3, 4, 5]


def test_full_output_for_other_readers(tmp_path):
    reader = {"id": "audit", "input": "route", "fn": "tests.test_route:tag"}
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline([reader]), store=store).run()
    assert [x["id"] for x in r.outputs["audit"]] == [1, 2, 3]


def test_budget_counts_router_calls(tmp_path):
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline(), store=store).run()
    assert r.spend["route"]["llm_calls"] == 5
    assert r.spend["route"]["input_tokens"] == 60  # the fake's invalid answers carry no tokens


def test_budget_stops_the_router(tmp_path):
    spec = pipeline()
    spec.budget = BudgetSpec(max_tokens=30)
    with Store(tmp_path / "s.db") as store:
        r = Runner(spec, store=store).run()
        types = [e["type"] for e in store.events(r.run_id)]
    assert r.status == "failed" and "budget.exceeded" in types
    assert "extract_invoice" not in r.outputs


def _bad(**change):
    raw = {"name": "x", "steps": steps()}
    for s in raw["steps"]:
        if s["id"] == "route":
            s.update(change)
    return raw


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"routes": {}}, "routes is empty"),
        ({"routes": {"bad-label": "extract_invoice"}}, "identifier"),
        ({"routes": {"invoice": "nope"}}, "does not exist"),
        ({"routes": {"invoice": "route"}}, "router itself"),
        ({"rules": [{"when": "type == 'nfe'", "route": "zzz"}]}, "not a label"),
        ({"rules": [{"when": "type ==", "route": "invoice"}]}, "rule when"),
        ({"rules": [{"compute": {"a": "1"}}]}, "router rule"),
        ({"output": {"x": "str"}}, "router has no 'output'"),
        ({"critic": {"criteria": "ok"}}, "no critic"),
    ],
)
def test_validation_errors(change, message):
    with pytest.raises((SpecError, ValueError), match=message):
        parse(_bad(**change))


def test_target_must_read_the_router():
    raw = _bad()
    raw["steps"][3]["input"] = "docs"
    with pytest.raises((SpecError, ValueError), match="must read the router"):
        parse(raw)


def test_route_tag_needs_routes():
    with pytest.raises((SpecError, ValueError), match="route_tag goes with routes"):
        parse({"name": "x", "steps": [{"id": "a", "fn": "m:f", "route_tag": True}]})


def test_cycle_through_a_route_is_rejected():
    raw = _bad()
    raw["steps"][1]["input"] = "extract_invoice"
    with pytest.raises((SpecError, ValueError), match="cycle"):
        parse(raw)


def test_mermaid_has_routed_edges():
    text = mermaid(pipeline())
    assert "route -->|invoice| extract_invoice" in text
    assert "route -->|contract| extract_contract" in text
    assert "docs --> route" in text
    g = parse_graph(text)
    assert ["route", "extract_invoice"] in g["edges"]
    assert {n["id"]: n["kind"] for n in g["nodes"]}["route"] == "agent"


def test_lint_ok_and_unknown_target():
    raw = {"name": "router", "steps": steps()}
    report = lint.check_text(yaml.safe_dump(raw, sort_keys=False))
    assert report.errors == []
    raw["steps"][1]["routes"]["invoice"] = "extract_invoce"
    report = lint.check_text(yaml.safe_dump(raw, sort_keys=False))
    assert any("extract_invoce" in f.message for f in report.errors)
