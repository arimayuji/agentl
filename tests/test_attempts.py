"""Agent attempts with validator feedback and progressive strictness (#198)."""

import pytest
from pydantic import ValidationError

from agentl import parse, register_provider
from agentl.llm import InvalidOutput, Result, lenient_check, schema_model, validation_errors
from agentl.runner import Runner
from agentl.spec import SpecError
from agentl.store import Store

SEEN: list[tuple[str, str, str]] = []  # (model, record, instruction)
FAILS = {"n": 0}


class Flaky:
    """Answers with a wrong type `FAILS["n"]` times per record, then correctly. The strong model
    returns a list with one bad item when the record asks for it."""

    def __init__(self, spec):
        self.name = spec.model

    def structured(self, instruction, record, schema):
        SEEN.append((self.name, record["t"], instruction))
        tries = sum(1 for _, t, _ in SEEN if t == record["t"])
        if tries <= FAILS["n"]:
            try:
                schema_model("x", {"amount": "float", "tags": "list[int]"}).model_validate(
                    {"amount": "not a number", "tags": []}
                )
            except ValidationError as e:
                raise InvalidOutput(str(e), 100, 5) from e
        tags = [1, "two", 3] if record.get("messy") else [1]
        return Result(schema.model_validate({"amount": 1.5, "tags": tags}), 100, 5)

    def text(self, instruction, content):
        return ""


register_provider("attempts-fake", Flaky)


def rows(input=None):
    return [{"t": "a"}]


def messy(input=None):
    return [{"t": "m", "messy": True}]


def pipeline(attempts, src="tests.test_attempts:rows", **step):
    return parse(
        {
            "name": "attempts",
            "llm": {
                "small": {"provider": "attempts-fake", "model": "small", "price": {"input": 1, "output": 1}},
                "large": {
                    "provider": "attempts-fake",
                    "model": "large",
                    "price": {"input": 10, "output": 10},
                },
            },
            "steps": [
                {"id": "src", "fn": src},
                {
                    "id": "rows",
                    "input": "src",
                    "agent": "extract",
                    "output": {"amount": "float", "tags": "list[int]"},
                    "llm": "small",
                    "concurrency": 1,
                    "attempts": attempts,
                    **step,
                },
            ],
        }
    )


THREE = [
    {"llm": "small", "validate": "strict"},
    {"llm": "small", "validate": "strict", "feedback": True},
    {"llm": "large", "validate": "lenient"},
]


@pytest.fixture(autouse=True)
def _reset():
    SEEN.clear()
    FAILS["n"] = 0


def test_second_attempt_gets_structured_feedback(tmp_path):
    FAILS["n"] = 1
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline(THREE), store=store, use_cache=False).run()
        events = [e for e in store.events(r.run_id) if e["type"] == "agent.attempt"]
    assert r.status == "succeeded" and r.outputs["rows"][0]["amount"] == 1.5
    assert [m for m, _, _ in SEEN] == ["small", "small"]
    assert SEEN[0][2] == "extract"
    fb = SEEN[1][2]
    assert "rejected by the validator" in fb and '"field": "amount"' in fb and "float_parsing" in fb
    assert len(events) == 1 and events[0]["data"]["attempt"] == 1
    assert events[0]["data"]["input_tokens"] == 100
    per = r.spend["rows"]["attempts"]
    assert per["1"] == {"llm": "small", "calls": 1, "failed": 1, "input_tokens": 100, "output_tokens": 5}
    assert per["2"]["failed"] == 0 and r.spend["rows"]["answered_by"] == {"small": 1}


def test_escalates_to_large_model_and_prices_it(tmp_path):
    FAILS["n"] = 2
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline(THREE), store=store, use_cache=False).run()
    assert [m for m, _, _ in SEEN] == ["small", "small", "large"]
    assert SEEN[2][2] == "extract"  # no feedback on the last attempt
    assert r.spend["rows"]["answered_by"] == {"large": 1}
    assert r.cost_usd == pytest.approx((2 * 105 + 1050) / 1e6)


def test_all_attempts_fail_goes_to_quarantine(tmp_path):
    FAILS["n"] = 9
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline(THREE), store=store, use_cache=False).run()
        q = store.rejected_list("attempts", "rows")
    assert r.outputs["rows"] == [] and len(SEEN) == 3
    assert len(q) == 1 and "amount" in q[0]["record"]["_error"]


def test_lenient_last_attempt_drops_invalid_items(tmp_path):
    FAILS["n"] = 2
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline(THREE, src="tests.test_attempts:messy"), store=store, use_cache=False).run()
        q = store.rejected_list("attempts", "rows")
        types = [e["type"] for e in store.events(r.run_id)]
    assert r.outputs["rows"][0]["tags"] == [1, 3]
    assert len(q) == 1 and q[0]["record"]["_value"] == "two" and "tags.1" in q[0]["record"]["_error"]
    assert "item.dropped" in types


def test_strict_attempt_rejects_the_bad_item():
    with pytest.raises(ValidationError):
        schema_model("s", {"tags": "list[int]"}).model_validate({"tags": [1, "two"]})


def test_lenient_check_required_field_still_fails():
    schema = schema_model("s", {"amount": "float", "note": "str?"})
    out, dropped = lenient_check(schema, {"amount": 2, "note": {"x": 1}})
    assert out == {"amount": 2.0, "note": None} and dropped[0]["field"] == "note"
    with pytest.raises(InvalidOutput) as e:
        lenient_check(schema, {"amount": "x"})
    assert validation_errors(e.value)[0]["field"] == "amount"


def test_attempts_validation():
    with pytest.raises(SpecError, match="only the last attempt can be lenient"):
        pipeline([{"validate": "lenient"}, {}])
    with pytest.raises(SpecError, match="unknown llm 'nope'"):
        pipeline([{"llm": "nope"}])
    with pytest.raises(SpecError, match="llm of each attempt"):
        pipeline([{}], llm=["small", "large"])


def mixed(input=None):
    return [{"t": "clean", "clean": True}, {"t": "a"}, {"t": "clean2", "clean": True}]


def clean_rule(record):
    if not record.get("clean"):
        raise ValueError("not clean")
    return {"amount": 2.0, "tags": [7]}


def test_hybrid_rules_with_attempts(tmp_path):
    """Rows the rules handle never reach the llm; the rest go through the attempts loop."""
    FAILS["n"] = 1
    spec = pipeline(THREE, src="tests.test_attempts:mixed", rules=[{"fn": "tests.test_attempts:clean_rule"}])
    with Store(tmp_path / "s.db") as store:
        r = Runner(spec, store=store, use_cache=False).run()
    out = r.outputs["rows"]
    assert r.status == "succeeded"
    assert [o["t"] for o in out] == ["clean", "a", "clean2"]
    assert [o["_path"] for o in out] == ["rules", "agent", "rules"]
    assert out[0]["amount"] == 2.0 and out[1]["amount"] == 1.5
    assert [(m, t) for m, t, _ in SEEN] == [("small", "a"), ("small", "a")]
    assert "rejected by the validator" in SEEN[1][2]
    assert r.spend["rows"]["paths"] == {"rules": 2, "agent": 1}
    assert r.spend["rows"]["attempts"]["1"]["failed"] == 1
