"""Critic loop on an agent step (#299)."""

import pytest

from agentl import parse, register_provider
from agentl.llm import Result
from agentl.runner import Runner
from agentl.spec import SpecError
from agentl.store import Store

CALLS: list[tuple[str, str]] = []  # (model, "gen" | "critic")
VERDICTS: list[bool] = []  # what the critic answers, in order (then: approve)


class Fake:
    """The generator answers total = 10 + number of earlier generations for the record; the
    critic pops the next verdict from VERDICTS."""

    def __init__(self, spec):
        self.name = spec.model

    def structured(self, instruction, record, schema):
        if "approved" in schema.model_fields:
            CALLS.append((self.name, "critic"))
            ok = VERDICTS.pop(0) if VERDICTS else True
            fb = "" if ok else f"total {record['answer']['total']} does not match the line items"
            return Result(schema.model_validate({"approved": ok, "feedback": fb}), 100, 10)
        CALLS.append((self.name, "gen"))
        n = sum(1 for _, kind in CALLS if kind == "gen") - 1
        return Result(schema.model_validate({"total": 10 + n}), 50, 5)

    def text(self, instruction, content):
        return ""


register_provider("critic-fake", Fake)


def rows(input=None):
    return [{"doc": "invoice 1"}]


def pipeline(critic=None, budget=None, **step):
    raw = {
        "name": "critic",
        "llm": {
            "default": {"provider": "critic-fake", "model": "gen", "price": {"input": 1, "output": 1}},
            "reviewer": {"provider": "critic-fake", "model": "rev", "price": {"input": 2, "output": 2}},
        },
        "steps": [
            {"id": "src", "fn": "tests.test_critic:rows"},
            {
                "id": "extract",
                "input": "src",
                "agent": "Extract invoice fields.",
                "output": {"total": "float"},
                "critic": critic
                if critic is not None
                else {"llm": "reviewer", "criteria": "Totals must match the line items."},
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
    VERDICTS.clear()


def _events(store, run_id):
    return [e["data"] for e in store.events(run_id) if e["type"] == "critic.round"]


def test_approved_first_round(tmp_path):
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline(), store=store).run()
        ev = _events(store, r.run_id)
    assert r.status == "succeeded" and r.outputs["extract"] == [{"doc": "invoice 1", "total": 10}]
    assert CALLS == [("gen", "gen"), ("rev", "critic")]
    assert len(ev) == 1 and ev[0]["round"] == 1 and ev[0]["approved"] is True
    assert r.spend["extract"]["llm_calls"] == 2
    assert r.spend["extract"]["models"]["rev"]["llm_calls"] == 1
    assert r.spend["extract"]["critic"] == {"approved": 1, "rejected": 0, "quarantined": 0}


def test_reject_then_approve_regenerates_with_feedback(tmp_path):
    VERDICTS.extend([False, True])
    seen = []
    orig = Fake.structured

    def spy(self, instruction, record, schema):
        seen.append(instruction)
        return orig(self, instruction, record, schema)

    Fake.structured = spy
    try:
        with Store(tmp_path / "s.db") as store:
            r = Runner(pipeline(), store=store).run()
            ev = _events(store, r.run_id)
    finally:
        Fake.structured = orig
    assert r.outputs["extract"][0]["total"] == 11
    assert [k for _, k in CALLS] == ["gen", "critic", "gen", "critic"]
    regen = seen[2]
    assert "Previous answer" in regen and "does not match the line items" in regen
    assert [(e["round"], e["approved"]) for e in ev] == [(1, False), (2, True)]
    assert "does not match" in ev[0]["feedback"] and "doc" not in ev[0]
    assert r.spend["extract"]["llm_calls"] == 4


def test_never_approved_is_quarantined(tmp_path):
    VERDICTS.extend([False, False, False])
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline(), store=store).run()
        ev = _events(store, r.run_id)
    assert r.outputs["extract"] == []
    bad = r.rejected["extract"][0]
    assert bad["_error"].startswith("critic did not approve after 2 rounds: total 11")
    assert [x["round"] for x in bad["_critic"]] == [1, 2]
    assert [k for _, k in CALLS] == ["gen", "critic", "gen", "critic"]
    assert len(ev) == 2 and not any(e["approved"] for e in ev)
    assert r.spend["extract"]["critic"]["quarantined"] == 1


def test_critic_defaults_to_step_llm(tmp_path):
    with Store(tmp_path / "s.db") as store:
        Runner(pipeline(critic={"criteria": "ok", "max_rounds": 1}), store=store).run()
    assert CALLS == [("gen", "gen"), ("gen", "critic")]


def test_second_run_hits_the_cache(tmp_path):
    VERDICTS.extend([False, True])
    with Store(tmp_path / "s.db") as store:
        Runner(pipeline(), store=store).run()
        CALLS.clear()
        VERDICTS.extend([False, False])  # would quarantine if asked again
        r = Runner(pipeline(), store=store).run()
        ev = _events(store, r.run_id)
    assert CALLS == []
    assert r.outputs["extract"][0]["total"] == 11 and r.spend["extract"]["llm_calls"] == 0
    assert all(e["cached"] for e in ev) and len(ev) == 2


def test_critic_calls_count_for_budget(tmp_path):
    VERDICTS.extend([False, False])
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline(budget={"max_tokens": 200}), store=store, use_cache=False).run()
    assert r.status == "failed" and "max_tokens" in (r.error or "")
    assert len(CALLS) < 4


def test_works_with_records_per_call(tmp_path):
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline(records_per_call=5), store=store).run()
    assert r.status == "succeeded" and r.outputs["extract"][0]["total"] is not None


@pytest.mark.parametrize(
    "critic,step,msg",
    [
        ({"criteria": "x", "llm": "nope"}, {}, "unknown llm 'nope'"),
        ({"criteria": "x", "max_rounds": 0}, {}, "max_rounds"),
        ({"criteria": "x", "max_rounds": 6}, {}, "max_rounds"),
        ({"criteria": "  "}, {}, "criteria"),
        ({"criteria": "x"}, {"output_mode": "rows"}, "output_mode rows"),
    ],
)
def test_validation(critic, step, msg):
    with pytest.raises((SpecError, ValueError), match=msg):
        pipeline(critic=critic, **step)


def test_only_agent_steps():
    with pytest.raises((SpecError, ValueError), match="only agent steps take a critic"):
        parse(
            {
                "name": "x",
                "steps": [{"id": "src", "fn": "tests.test_critic:rows", "critic": {"criteria": "x"}}],
            }
        )
