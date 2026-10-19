"""Human review queue: uncertain agent answers wait for a person, and the answer is reused (#153)."""

import json

import pytest
import yaml

from agentl import llm
from agentl.cli import main
from agentl.runner import Runner
from agentl.spec import SpecError, load, parse
from agentl.stats import compute
from agentl.store import Store


class Unsure:
    """Fake model: sure about flights, unsure about anything else. Counts its calls."""

    calls = 0

    def __init__(self, spec):
        self.spec = spec

    def structured(self, instruction, record, schema):
        Unsure.calls += 1
        sure = "flight" in record["text"].lower()
        value = {"category": "travel" if sure else "other", "confidence": 0.95 if sure else 0.4}
        return llm.Result(schema.model_validate(value), input_tokens=10, output_tokens=5)


PIPELINE = """
name: expenses
llm:
  default: {{provider: unsure, model: m}}
steps:
  - id: load
    use: json.read
    with: {{path: data.json}}
  - id: classify
    input: load
    agent: Classify the expense.
    output: {{category: str, confidence: float}}
    review: {review}
  - id: after
    input: classify
    fn: tests.test_review:passthrough
"""


def passthrough(records):
    return list(records)


@pytest.fixture
def project(tmp_path, monkeypatch):
    llm.register_provider("unsure", Unsure)
    Unsure.calls = 0
    rows = [{"text": "Flight to Lisbon"}, {"text": "Taxi to airport"}]
    (tmp_path / "data.json").write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / "x.db")

    def run(review="{below: 0.8}", **kw):
        (tmp_path / "pipeline.yaml").write_text(PIPELINE.format(review=review), encoding="utf-8")
        spec = load(tmp_path / "pipeline.yaml")
        return Runner(spec, store=store, **kw).run()

    yield run, store, str(tmp_path / "x.db")
    store.close()


def test_low_confidence_is_held_and_queued(project):
    run, store, _ = project
    result = run()
    assert result.status == "succeeded"
    assert [r["text"] for r in result.outputs["after"]] == ["Flight to Lisbon"]
    assert result.spend["classify"]["review"]["held"] == 1
    [item] = store.review_list()
    assert item["input"] == {"text": "Taxi to airport"}
    assert item["answer"] == {"category": "other", "confidence": 0.4}
    assert item["reason"] == "confidence 0.4 below 0.8"
    assert item["run_id"] == result.run_id
    pending = [e for e in store.events(result.run_id) if e["type"] == "review.pending"]
    assert pending[0]["data"] == {"queued": 1, "held": 1, "flagged": 0}
    # the same input again is held, and not asked twice
    again = run()
    assert len(again.outputs["after"]) == 1
    assert len(store.review_list()) == 1
    assert again.spend["classify"]["review"]["queued"] == 0


def test_hold_false_marks_and_passes_on(project):
    run, store, _ = project
    result = run("{below: 0.8, hold: false}")
    marked = [r for r in result.outputs["after"] if r.get("_review") == "pending"]
    assert [r["text"] for r in marked] == ["Taxi to airport"]
    assert len(result.outputs["after"]) == 2
    assert len(store.review_list()) == 1


def test_when_and_always(project):
    run, store, _ = project
    run("{when: \"category == 'travel'\"}")
    assert [r["answer"]["category"] for r in store.review_list()] == ["travel"]
    store.close()


def test_fix_is_reused_without_llm_and_exported_as_golden(project, capsys, tmp_path):
    run, store, db = project
    run()
    [item] = store.review_list()
    assert main(["review", "list", "--db", db]) == 0
    assert "Taxi to airport" not in capsys.readouterr().out  # the list shows the answer, not input
    assert main(["review", "fix", str(item["id"]), "category=transport", "--db", db]) == 0
    assert main(["review", "fix", str(item["id"]), "nope=1", "--db", db]) == 2
    calls = Unsure.calls
    result = run()
    assert Unsure.calls == calls  # cache hit for the flight, the person's answer for the taxi
    taxi = next(r for r in result.outputs["after"] if r["text"] == "Taxi to airport")
    assert taxi["category"] == "transport"
    assert result.spend["classify"]["review"]["reused"] == 1
    # without the cache the flight is asked again, the taxi still is not
    run(use_cache=False)
    assert Unsure.calls == calls + 1

    golden = tmp_path / "golden" / "classify.jsonl"
    assert main(["review", "export", "--golden", str(golden), "--db", db]) == 0
    assert main(["review", "export", "--golden", str(golden), "--db", db]) == 0
    lines = [json.loads(x) for x in golden.read_text().splitlines()]
    assert lines == [{"text": "Taxi to airport", "category": "transport"}]


def test_accept_reject_show(project, capsys):
    run, store, db = project
    run("{always: true}")
    first, second = store.review_list()
    assert main(["review", "show", str(first["id"]), "--db", db]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pending"
    assert main(["review", "accept", str(first["id"]), "--db", db]) == 0
    assert main(["review", "reject", str(second["id"]), "--db", db]) == 0
    assert main(["review", "show", "999", "--db", db]) == 1
    result = run("{always: true}")
    assert [r["text"] for r in result.outputs["after"]] == ["Flight to Lisbon"]
    assert result.spend["classify"]["review"] == {
        "queued": 0,
        "held": 0,
        "flagged": 0,
        "reused": 1,
        "dropped": 1,
    }
    report = compute(store, 0)
    [p] = report["pipelines"]
    assert p["review"] == {"pending": 0, "reviewed": 2}
    [agent] = p["agents"]
    assert agent["review_held"] == 2 and agent["review_reused"] == 2


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ("agent: x\n    output: {category: str}\n    review: {below: 0.8}", "confidence: float"),
        ("agent: x\n    output: {category: str}\n    review: {}", "needs below"),
        ("agent: x\n    output: {category: str}\n    review: {when: 'a +'}", "review when"),
        ("agent: x\n    output: {category: str}\n    review: check it", "is a mapping"),
        ("use: json.read\n    with: {path: a.json}\n    review: {always: true}", "only agent steps"),
    ],
)
def test_spec_validation(step, message):
    text = f"name: p\nsteps:\n  - id: s\n    {step}\n"
    with pytest.raises(SpecError, match=message):
        parse(yaml.safe_load(text))


def test_review_answer_survives_a_prompt_version_change(project, monkeypatch):
    run, store, _ = project
    run()
    [item] = store.review_list()
    store.review_resolve(item["id"], "fixed", {"category": "transport", "confidence": 1.0})
    monkeypatch.setattr(llm, "PROMPT_VERSION", "upgraded")
    calls = Unsure.calls
    result = run()
    taxi = next(r for r in result.outputs["after"] if r["text"] == "Taxi to airport")
    assert taxi["category"] == "transport"
    assert Unsure.calls == calls + 1  # only the flight is asked again under the new prompts
