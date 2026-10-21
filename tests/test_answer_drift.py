"""Answer drift: an agent step that starts answering differently emits `step.drift` (#155)."""

import json

import pytest
import yaml

from agentl import answers, llm
from agentl.cli import main
from agentl.runner import Runner
from agentl.spec import SpecError, load, parse
from agentl.stats import compute, text
from agentl.store import Store


class Shifty:
    """Fake model: labels every expense with `Shifty.category` and amount `Shifty.amount`."""

    category = "travel"
    amount = 10.0

    def __init__(self, spec):
        self.spec = spec

    def structured(self, instruction, record, schema):
        value = {"category": Shifty.category, "amount": Shifty.amount, "note": record["text"]}
        return llm.Result(schema.model_validate(value), input_tokens=10, output_tokens=5)


PIPELINE = """
name: expenses
llm:
  default: {{provider: shifty, model: m}}
steps:
  - id: load
    use: json.read
    with: {{path: data.json}}
  - id: classify
    input: load
    agent: Classify the expense.
    output: {{category: str, amount: float, note: str}}
    cache: false
    drift: {drift}
notify:
  channels: {{ops: {{type: console}}}}
  rules: [{{on: step.drift, to: [ops]}}]
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    llm.register_provider("shifty", Shifty)
    Shifty.category, Shifty.amount = "travel", 10.0
    rows = [{"text": f"expense {i}"} for i in range(4)]
    (tmp_path / "data.json").write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / "x.db")

    def run(drift="{window: 3, threshold: 0.15}"):
        (tmp_path / "pipeline.yaml").write_text(PIPELINE.format(drift=drift), encoding="utf-8")
        return Runner(load(tmp_path / "pipeline.yaml"), store=store).run()

    yield run, store, str(tmp_path / "x.db")
    store.close()


def _drifts(store, run_id):
    return [e for e in store.events(run_id) if e["type"] == "step.drift"]


def test_stable_answers_emit_nothing_and_a_shift_emits_step_drift(project, capsys):
    run, store, _ = project
    first = run()
    assert first.status == "succeeded"
    assert not _drifts(store, first.run_id)  # no history yet
    assert not _drifts(store, run().run_id)
    Shifty.category, Shifty.amount = "food", 20.0
    shifted = run()
    [event] = _drifts(store, shifted.run_id)
    changes = {c["metric"]: c for c in event["data"]["changes"]}
    assert changes["share(category=travel)"]["before"] == 1.0
    assert changes["share(category=travel)"]["after"] == 0.0
    assert changes["share(category=food)"]["after"] == 1.0
    assert changes["mean(amount)"]["change"] == 1.0
    assert "note" not in json.dumps(event["data"]["changes"])  # free text has no distribution
    assert event["data"]["window"] == 2
    out = capsys.readouterr().out
    assert "step.drift step classify" in out and "share(category=food): 0% -> 100%" in out
    assert "changes=" not in out


def test_below_threshold_is_quiet(project):
    run, store, _ = project
    run()
    Shifty.amount = 11.0  # +10%, under 15%
    assert not _drifts(store, run().run_id)


def test_reject_and_review_rates():
    rejected = answers.profile([{"x": 1}], {"x": "int"}, rejected=3, review_queued=1, cost_usd=0.4)
    assert rejected["reject_rate"] == 0.75 and rejected["review_rate"] == 0.25
    assert rejected["cost_per_record"] == 0.1
    base = answers.profile([{"x": 1}] * 4, {"x": "int"}, rejected=0, review_queued=0, cost_usd=0.4)
    changes = {c["metric"]: c for c in answers.compare(rejected, [base, base], 0.15)}
    assert changes["reject_rate"]["change"] == 0.75 and changes["reject_rate"]["kind"] == "points"
    assert changes["review_rate"]["after"] == 0.25
    assert "cost_per_record" not in changes and "mean(x)" not in changes


def test_cost_per_record_relative():
    a = answers.profile([{}] * 2, {}, 0, 0, 0.2)
    b = answers.profile([{}] * 2, {}, 0, 0, 0.1)
    [c] = answers.compare(a, [b], 0.15)
    assert c["metric"] == "cost_per_record" and c["change"] == 1.0 and c["kind"] == "relative"


def test_min_runs_waits_for_history(project):
    run, store, _ = project
    run("{window: 3, min_runs: 2}")
    Shifty.category = "food"
    assert not _drifts(store, run("{window: 3, min_runs: 2}").run_id)


def test_spec_validation():
    base = "name: p\nsteps:\n  - {id: a, use: json.read, with: {path: x}, drift: true}\n"
    with pytest.raises(SpecError, match="drift belongs to agent steps"):
        parse(yaml.safe_load(base))
    agent = (
        "name: p\nllm: {default: {provider: echo, model: m}}\nsteps:\n"
        "  - {id: a, agent: x, output: {c: str}, drift: %s}\n"
    )
    assert parse(yaml.safe_load(agent % "true")).steps[0].drift.window == 7
    assert parse(yaml.safe_load(agent % "false")).steps[0].drift is None
    with pytest.raises(SpecError, match="min_runs"):
        parse(yaml.safe_load(agent % "{window: 2, min_runs: 3}"))


def test_stats_shows_the_comparison(project, capsys):
    run, store, db = project
    run()
    run()
    Shifty.category = "food"
    run()
    report = compute(store, 0)
    [row] = report["pipelines"][0]["drift"]
    assert row["step"] == "classify" and row["window"] == 2
    assert {c["metric"] for c in row["changes"]} >= {"share(category=food)", "share(category=travel)"}
    assert row["baseline"]["share(category=travel)"] == 1.0
    assert "answer drift vs the last 2 runs" in text(report)
    capsys.readouterr()
    assert main(["stats", "--db", db, "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["pipelines"][0]["drift"][0]["changes"]
