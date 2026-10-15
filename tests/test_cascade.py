"""Model cascade and input dedup in agent steps (#67)."""

import pytest

from agentl import parse, register_provider
from agentl.llm import InvalidOutput, Result
from agentl.runner import Runner
from agentl.spec import SpecError
from agentl.store import Store

CALLS: list[tuple[str, str]] = []


class Cheap:
    """Fails on records marked hard, low confidence on records marked unsure."""

    def __init__(self, spec):
        self.name = spec.model

    def structured(self, instruction, record, schema):
        CALLS.append((self.name, record["t"]))
        if self.name == "cheap" and record.get("hard"):
            raise InvalidOutput("bad json", 10, 1)
        conf = 0.3 if self.name == "cheap" and record.get("unsure") else 0.9
        return Result(schema.model_validate({"label": self.name, "confidence": conf}), 10, 2)

    def text(self, instruction, content):
        return ""


register_provider("cascade-fake", Cheap)


def rows(input=None):
    return [
        {"t": "a"},
        {"t": "b", "hard": True},
        {"t": "c", "unsure": True},
        {"t": "a"},  # same as the first: asked once
    ]


def pipeline(**step):
    return parse(
        {
            "name": "cascade",
            "llm": {
                "cheap": {"provider": "cascade-fake", "model": "cheap", "price": {"input": 1, "output": 1}},
                "strong": {
                    "provider": "cascade-fake",
                    "model": "strong",
                    "price": {"input": 10, "output": 10},
                },
            },
            "steps": [
                {"id": "src", "fn": "tests.test_cascade:rows"},
                {
                    "id": "tag",
                    "input": "src",
                    "agent": "label",
                    "output": {"label": "str", "confidence": "float"},
                    "llm": ["cheap", "strong"],
                    "concurrency": 1,
                    **step,
                },
            ],
        }
    )


def test_cascade_escalates_invalid_and_unsure_records_and_dedups(tmp_path):
    CALLS.clear()
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline(escalate_below=0.5), store=store).run()
        events = [e["type"] for e in store.events(r.run_id)]
    assert r.status == "succeeded"
    assert [x["label"] for x in r.outputs["tag"]] == ["cheap", "strong", "strong", "cheap"]
    # a: cheap once (deduped); b: cheap twice (retry) then strong; c: cheap, then strong
    assert CALLS.count(("cheap", "a")) == 1 and CALLS.count(("cheap", "b")) == 2
    assert ("strong", "b") in CALLS and ("strong", "c") in CALLS and ("strong", "a") not in CALLS
    assert r.spend["tag"]["answered_by"] == {"cheap": 1, "strong": 2}
    assert "agent.deduplicated" in events
    assert r.cost_usd == pytest.approx((12 + 2 * 11 + 12 + 2 * 120) / 1e6)  # each call at its own price


def test_without_escalate_below_low_confidence_is_kept(tmp_path):
    CALLS.clear()
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline(), store=store).run()
    assert [x["label"] for x in r.outputs["tag"]] == ["cheap", "strong", "cheap", "cheap"]


def test_cascade_answers_are_cached_across_runs(tmp_path):
    with Store(tmp_path / "s.db") as store:
        Runner(pipeline(), store=store).run()
        CALLS.clear()
        r = Runner(pipeline(), store=store).run()
    assert CALLS == [] and r.outputs["tag"][1]["label"] == "strong"


def test_cascade_validation():
    with pytest.raises(SpecError, match="unknown llm 'nope'"):
        pipeline(llm=["cheap", "nope"])
    with pytest.raises(SpecError, match="mode batch"):
        pipeline(mode="batch")
    with pytest.raises(SpecError, match="needs a 'confidence: float'"):
        pipeline(escalate_below=0.5, output={"label": "str"})
    with pytest.raises(SpecError, match="needs a list of llms"):
        pipeline(llm="cheap", escalate_below=0.5)
