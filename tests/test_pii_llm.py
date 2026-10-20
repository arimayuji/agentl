"""Personal data before the llm call (#337): `pii: {before_llm: warn|mask|block|off}` on agent
steps, with a fake provider that records every prompt it gets."""

import json
import sqlite3
from typing import ClassVar

import pytest

from agentl import connector, lint, parse
from agentl.llm import Result, register_provider
from agentl.pii import Masker
from agentl.runner import Runner
from agentl.spec import SpecError
from agentl.store import Store

EMAIL, CPF, PHONE = "ana@example.com", "529.982.247-25", "(11) 98765-4321"
ROWS = [
    {"id": 1, "email": EMAIL, "tax_id": CPF, "body": f"Call me at {PHONE} or write to {EMAIL}."},
    {"id": 2, "email": "bo@example.org", "tax_id": "", "body": "No personal data here."},
]
RAW = (EMAIL, CPF, PHONE, "bo@example.org", "98765-4321")


class Recorder:
    """Answers with a reply that repeats the placeholders it was given, and keeps each prompt."""

    prompts: ClassVar[list[str]] = []

    def __init__(self, spec):
        pass

    def structured(self, instruction, record, schema):
        Recorder.prompts.append(json.dumps([instruction, record]))
        reply = f"Wrote to {record.get('email')} about {record.get('body')}"
        return Result(schema.model_validate({"reply": reply}), 10, 2)


@pytest.fixture(autouse=True)
def _provider():
    register_provider("recorder", Recorder)
    Recorder.prompts = []

    @connector("test.pii_rows")
    def rows(input):
        return [dict(r) for r in ROWS]


def pipeline(pii=None, **step):
    agent = {"id": "a", "agent": "Answer the ticket.", "input": "src", "output": {"reply": "str"}}
    if pii is not None:
        agent["pii"] = pii
    return parse(
        {
            "name": "pii",
            "llm": {"provider": "recorder"},
            "steps": [{"id": "src", "use": "test.pii_rows"}, {**agent, **step}],
        }
    )


def test_mask_sends_no_raw_values_and_restores_them(tmp_path):
    with Store(tmp_path / "s.db") as store:
        res = Runner(pipeline({"before_llm": "mask"}), store=store).run()
        assert res.status == "succeeded", res.error
        sent = "\n".join(Recorder.prompts)
        assert len(Recorder.prompts) == 2
        for raw in RAW:
            assert raw not in sent
        assert "[EMAIL_1]" in sent and "[CPF_1]" in sent and "[PHONE_1]" in sent
        first, second = res.outputs["a"]
        assert first["email"] == EMAIL and first["tax_id"] == CPF  # passed-through fields
        assert first["reply"] == f"Wrote to {EMAIL} about Call me at {PHONE} or write to {EMAIL}."
        assert second["reply"].startswith("Wrote to bo@example.org")
        events = store.events(res.run_id)
        found = [e for e in events if e["type"] == "pii.detected"]
        assert len(found) == 1 and found[0]["data"]["action"] == "mask"
        assert found[0]["data"]["kinds"] == {"email": 3, "cpf": 1, "phone": 1}
        assert found[0]["data"]["fields"]["body"] == 2
        logged = json.dumps(events, default=str)
        for raw in RAW:
            assert raw not in logged
        # The cache keeps the answer with placeholders, never the raw values.
        con = sqlite3.connect(tmp_path / "s.db")
        cached = json.dumps([list(r) for r in con.execute("SELECT * FROM cache")], default=str)
        con.close()
        assert "[EMAIL_1]" in cached
        for raw in RAW:
            assert raw not in cached

        # A second run is answered from the cache, and the values come back the same way.
        Recorder.prompts = []
        again = Runner(pipeline({"before_llm": "mask"}), store=store).run()
        assert Recorder.prompts == []
        assert again.outputs["a"][0]["reply"] == first["reply"]


def test_mask_cache_key_ignores_the_raw_values(tmp_path):
    with Store(tmp_path / "s.db") as store:
        run = Runner(pipeline({"before_llm": "mask"}), store=store)
        Runner(pipeline({"before_llm": "mask"}), store=store).run()
        n = len(Recorder.prompts)
        ROWS[1]["email"] = "other@example.org"  # only the personal data changes
        try:
            out = run.run()
        finally:
            ROWS[1]["email"] = "bo@example.org"
        assert len(Recorder.prompts) == n  # same masked record, same cache key
        assert out.outputs["a"][1]["reply"].startswith("Wrote to other@example.org")


def test_warn_is_the_default_and_sends_the_record(tmp_path):
    with Store(tmp_path / "s.db") as store:
        res = Runner(pipeline(), store=store).run()
        assert EMAIL in "\n".join(Recorder.prompts)
        found = [e for e in store.events(res.run_id) if e["type"] == "pii.detected"]
        assert found and found[0]["data"]["action"] == "warn" and found[0]["data"]["records"] == 2
        assert EMAIL not in json.dumps(found)


def test_block_quarantines_records_with_personal_data(tmp_path):
    with Store(tmp_path / "s.db") as store:
        res = Runner(pipeline({"before_llm": "block", "kinds": ["cpf"]}), store=store).run()
        assert res.status == "succeeded", res.error
        assert len(Recorder.prompts) == 1 and CPF not in Recorder.prompts[0]
        assert [r["id"] for r in res.outputs["a"]] == [2]
        (bad,) = res.rejected["a"]
        assert bad["id"] == 1 and "tax_id (cpf)" in bad["_error"] and CPF not in bad["_error"]


def test_off_and_fields(tmp_path):
    with Store(tmp_path / "s.db") as store:
        res = Runner(pipeline("off"), store=store).run()
        assert not [e for e in store.events(res.run_id) if e["type"] == "pii.detected"]
        Recorder.prompts = []
        Runner(pipeline({"before_llm": "mask", "fields": ["email"]}, cache=False), store=store).run()
        sent = "\n".join(Recorder.prompts)
        assert EMAIL not in json.dumps([json.loads(p)[1]["email"] for p in Recorder.prompts])
        assert PHONE in sent  # `body` is not in `fields`


def test_spec_validates_pii():
    with pytest.raises(SpecError, match="unknown pii kinds"):
        pipeline({"before_llm": "mask", "kinds": ["dna"]})
    with pytest.raises(SpecError):
        pipeline({"before_llm": "hide"})
    with pytest.raises(SpecError, match="output_mode rows"):
        pipeline("mask", output_mode="rows")
    with pytest.raises(SpecError, match="agent steps"):
        parse({"name": "x", "steps": [{"id": "s", "use": "test.pii_rows", "pii": "mask"}]})


def test_masker_is_stable_and_round_trips():
    rec = {"a": EMAIL, "b": [f"to {EMAIL}", {"c": 52998224725}], "n": 7, "card": "4111 1111 1111 1111"}
    m1, m2 = Masker(), Masker()
    masked = m1.mask(rec)
    assert masked == m2.mask(dict(rec))
    assert masked["a"] == "[EMAIL_1]" and masked["b"][0] == "to [EMAIL_1]"
    assert masked["b"][1]["c"] == "[CPF_1]" and masked["card"] == "[CARD_1]"
    assert m1.restore(masked) == rec
    assert m1.restore("[EMAIL_9] stays") == "[EMAIL_9] stays"


LINT = """name: x
llm: { provider: echo }
steps:
  - id: src
    use: csv.read
    with: { path: in.csv }
  - id: label
    agent: label it
    input: src
    output: { label: str }
"""


def test_lint_warns_on_sampled_personal_data(tmp_path):
    (tmp_path / "in.csv").write_text(f"id,email,city\n1,{EMAIL},Recife\n2,bo@example.org,Natal\n")
    hit = [f for f in lint.check_text(LINT, base_dir=str(tmp_path)).findings if f.code == "AL012"]
    assert len(hit) == 1 and "email (email)" in hit[0].message and EMAIL not in hit[0].message
    with_pii = LINT + "    pii: mask\n"
    assert not [f for f in lint.check_text(with_pii, base_dir=str(tmp_path)).findings if f.code == "AL012"]
