"""Jury / consensus on an agent step (#299)."""

import pytest

from agentl import parse, register_provider
from agentl.llm import Result
from agentl.runner import Runner
from agentl.spec import SpecError
from agentl.store import Store

CALLS: list[str] = []  # model names, in call order
# What each model answers per record doc: {model: {doc: category}}; confidence 0.9 by default.
ANSWERS: dict[str, dict[str, str]] = {}
CONFIDENCE: dict[str, float] = {}  # doc -> confidence of the "first" model


class Fake:
    def __init__(self, spec):
        self.name = spec.model

    def structured(self, instruction, record, schema):
        CALLS.append(self.name)
        doc = record.get("doc") or record["record"]["doc"]
        cat = (
            ANSWERS[self.name][doc] if "answers" in record else ANSWERS.get(self.name, {}).get(doc, "food")
        )  # judge or juror
        value = {"category": cat}
        if "note" in schema.model_fields:
            value["note"] = f"by {self.name}"  # differs per juror
        if "confidence" in schema.model_fields:
            value["confidence"] = CONFIDENCE.get(doc, 0.9) if self.name == "first" else 0.9
        return Result(schema.model_validate(value), 100, 10)

    def text(self, instruction, content):
        return ""


register_provider("jury-fake", Fake)

DOCS = [{"doc": "lunch"}]


def rows(input=None):
    return list(DOCS)


def llm(model):
    return {"provider": "jury-fake", "model": model, "price": {"input": 1, "output": 1}}


def pipeline(jury=None, budget=None, output=None, **step):
    raw = {
        "name": "jury",
        "llm": {"default": llm("first"), **{m: llm(m) for m in ["a", "b", "c", "strong"]}},
        "steps": [
            {"id": "src", "fn": "tests.test_jury:rows"},
            {
                "id": "classify",
                "input": "src",
                "agent": "Classify the expense.",
                "output": output or {"category": "str"},
                "jury": jury if jury is not None else {"llms": ["a", "b", "c"], "when": "always"},
                **step,
            },
        ],
    }
    if budget:
        raw["budget"] = budget
    return parse(raw)


@pytest.fixture(autouse=True)
def _reset():
    CALLS.clear()
    ANSWERS.clear()
    CONFIDENCE.clear()
    DOCS[:] = [{"doc": "lunch"}]


def _decided(store, run_id):
    return [e["data"] for e in store.events(run_id) if e["type"] == "jury.decided"]


def test_unanimous(tmp_path):
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline(jury_tag=True), store=store).run()
        ev = _decided(store, r.run_id)
    assert r.outputs["classify"] == [{"doc": "lunch", "category": "food", "_agreement": 1.0}]
    assert sorted(CALLS) == ["a", "b", "c"]
    assert r.spend["classify"]["jury"] == {"unanimous": 1, "majority": 0, "judged": 0, "tied": 0}
    assert ev == [{"unanimous": 1, "majority": 0, "judged": 0, "tied": 0}]
    assert set(r.spend["classify"]["models"]) == {"a", "b", "c"}


def test_majority_two_to_one_normalizes_strings(tmp_path):
    ANSWERS.update({"a": {"lunch": "Food "}, "b": {"lunch": "food"}, "c": {"lunch": "travel"}})
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline(jury_tag=True), store=store).run()
    out = r.outputs["classify"][0]
    assert out["category"] == "Food " and out["_agreement"] == 0.67
    assert r.spend["classify"]["jury"]["majority"] == 1


def test_tie_is_quarantined(tmp_path):
    ANSWERS.update({"a": {"lunch": "food"}, "b": {"lunch": "travel"}, "c": {"lunch": "office"}})
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline(), store=store).run()
    assert r.outputs["classify"] == []
    bad = r.rejected["classify"][0]
    assert "no answer has a majority on category" in bad["_error"]
    assert set(bad["_jury"]) == {"a", "b", "c"}
    assert r.spend["classify"]["jury"]["tied"] == 1


def test_fields_limit_the_vote(tmp_path):
    out = {"category": "str", "note": "str"}
    ANSWERS.update({"a": {"lunch": "food"}, "b": {"lunch": "food"}})
    with Store(tmp_path / "s.db") as store:
        r = Runner(
            pipeline(jury={"llms": ["a", "b"], "when": "always", "fields": ["category"]}, output=out),
            store=store,
        ).run()
    assert r.outputs["classify"][0]["category"] == "food"


def test_judge_mode(tmp_path):
    ANSWERS.update(
        {
            "a": {"lunch": "food"},
            "b": {"lunch": "travel"},
            "c": {"lunch": "travel"},
            "strong": {"lunch": "meals"},
        }
    )
    jury = {"llms": ["a", "b", "c"], "decide": "judge", "judge": "strong", "when": "always"}
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline(jury=jury), store=store).run()
    assert r.outputs["classify"][0]["category"] == "meals"
    assert CALLS[-1] == "strong" and len(CALLS) == 4
    assert r.spend["classify"]["jury"]["judged"] == 1


def test_judge_skipped_when_unanimous(tmp_path):
    jury = {"llms": ["a", "b"], "decide": "judge", "judge": "strong", "when": "always"}
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline(jury=jury), store=store).run()
    assert "strong" not in CALLS and r.spend["classify"]["jury"]["unanimous"] == 1


def test_low_confidence_only(tmp_path):
    DOCS[:] = [{"doc": "lunch"}, {"doc": "taxi"}]
    CONFIDENCE.update({"lunch": 0.95, "taxi": 0.3})
    ANSWERS.update({"a": {"taxi": "travel"}, "b": {"taxi": "travel"}, "c": {"taxi": "food"}})
    jury = {"llms": ["a", "b", "c"]}
    out = {"category": "str", "confidence": "float"}
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline(jury=jury, output=out, escalate_below=0.7), store=store).run()
    by = {x["doc"]: x for x in r.outputs["classify"]}
    assert by["lunch"]["category"] == "food" and by["taxi"]["category"] == "travel"
    assert CALLS.count("first") == 2 and sorted(c for c in CALLS if c != "first") == ["a", "b", "c"]
    assert r.spend["classify"]["jury"]["majority"] == 1


def test_second_run_is_free(tmp_path):
    ANSWERS.update({"a": {"lunch": "food"}, "b": {"lunch": "food"}, "c": {"lunch": "x"}})
    with Store(tmp_path / "s.db") as store:
        Runner(pipeline(), store=store).run()
        CALLS.clear()
        r = Runner(pipeline(), store=store).run()
    assert CALLS == [] and r.spend["classify"]["llm_calls"] == 0
    assert r.outputs["classify"][0]["category"] == "food"


def test_juror_cache_survives_a_new_decision(tmp_path):
    ANSWERS.update(
        {"a": {"lunch": "food"}, "b": {"lunch": "x"}, "c": {"lunch": "x"}, "strong": {"lunch": "y"}}
    )
    with Store(tmp_path / "s.db") as store:
        Runner(pipeline(), store=store).run()
        CALLS.clear()
        jury = {"llms": ["a", "b", "c"], "decide": "judge", "judge": "strong", "when": "always"}
        r = Runner(pipeline(jury=jury), store=store).run()
    assert CALLS == ["strong"] and r.outputs["classify"][0]["category"] == "y"


def test_budget_counts_juror_calls(tmp_path):
    DOCS[:] = [{"doc": f"d{i}"} for i in range(5)]
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline(budget={"max_tokens": 250}, concurrency=1), store=store, use_cache=False).run()
    assert r.status == "failed" and "max_tokens" in (r.error or "")
    assert len(CALLS) < 15


@pytest.mark.parametrize(
    "jury,step,msg",
    [
        ({"llms": ["a"], "when": "always"}, {}, "at least 2"),
        ({"llms": ["a", "nope"], "when": "always"}, {}, "unknown llm 'nope'"),
        ({"llms": ["a", "a"], "when": "always"}, {}, "different"),
        ({"llms": ["a", "b"], "decide": "judge", "when": "always"}, {}, "needs judge"),
        ({"llms": ["a", "b"], "judge": "strong", "when": "always"}, {}, "only used with decide: judge"),
        ({"llms": ["a", "b"], "when": "always"}, {"output_mode": "rows"}, "output_mode rows"),
        ({"llms": ["a", "b"], "when": "always"}, {"records_per_call": 5}, "one record per call"),
        ({"llms": ["a", "b"], "when": "always", "fields": ["x"]}, {}, "not in output: x"),
        ({"llms": ["a", "b"]}, {}, "low_confidence needs a confidence"),
    ],
)
def test_validation(jury, step, msg):
    with pytest.raises((SpecError, ValueError), match=msg):
        pipeline(jury=jury, **step)


def test_only_agent_steps():
    with pytest.raises((SpecError, ValueError), match="only agent steps take a jury"):
        parse(
            {
                "name": "x",
                "steps": [
                    {
                        "id": "src",
                        "fn": "tests.test_jury:rows",
                        "jury": {"llms": ["a", "b"], "when": "always"},
                    }
                ],
            }
        )
