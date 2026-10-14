"""Prompt caching and multi-record calls (#59). No real LLM: fake providers and stubbed clients."""

import sys
import threading
import types
from typing import ClassVar

import pytest

from agentl import parse
from agentl.llm import Result, provider, register_provider, schema_model
from agentl.runner import Runner
from agentl.spec import LLMSpec, PriceSpec
from agentl.store import Store


def test_cost_prices_cache_reads_and_writes():
    spec = LLMSpec(price=PriceSpec(input=3, output=15, cache_read=0.3, cache_write=3.75))
    # 1M input of which 600k read and 100k written, plus 100k output
    assert spec.cost(1_000_000, 100_000, 600_000, 100_000) == pytest.approx(0.9 + 0.18 + 0.375 + 1.5)
    plain = LLMSpec(price=PriceSpec(input=3, output=15))
    assert plain.cost(1_000_000, 0, 600_000) == pytest.approx(3.0)  # no cache price: billed as input


class Batcher:
    """Answers every record in a batch; `broken` indexes come back invalid, `missing` are left out."""

    calls: ClassVar[list[int]] = []
    broken: ClassVar[set[int]] = set()
    missing: ClassVar[set[int]] = set()
    lock = threading.Lock()

    def __init__(self, spec):
        pass

    def structured(self, instruction, record, schema):
        with self.lock:
            self.calls.append(len(record.get("records", [record])))
        if "records" not in record:
            return Result(schema.model_validate({"double": record["n"] * 2}), 100, 10, 80)
        items = []
        for r in record["records"]:
            if r["index"] in self.missing:
                continue
            value = "oops" if r["index"] in self.broken else r["record"]["n"] * 2
            items.append({"index": r["index"], "double": value})
        return Result(schema.model_validate({"items": items}), 1000, 100, 800)

    def text(self, instruction, content):
        return ""


register_provider("batcher", Batcher)


def rows(input=None, n=10):
    return [{"n": i} for i in range(n)]


def pipeline(per_call=1, n=10):
    return parse(
        {
            "name": "eff",
            "llm": {
                "provider": "batcher",
                "model": "m",
                "price": {"input": 3, "output": 15, "cache_read": 0.3},
            },
            "steps": [
                {"id": "src", "fn": "tests.test_agent_efficiency:rows", "with": {"n": n}},
                {
                    "id": "a",
                    "agent": "Double n.",
                    "input": "src",
                    "records_per_call": per_call,
                    "concurrency": 2,
                    "output": {"double": "int"},
                },
            ],
        }
    )


@pytest.fixture(autouse=True)
def reset():
    Batcher.calls, Batcher.broken, Batcher.missing = [], set(), set()


def test_records_per_call_groups_records_and_keeps_order(tmp_path):
    with Store(tmp_path / "db") as store:
        r = Runner(pipeline(per_call=4), store=store, use_cache=False).run()
    assert r.status == "succeeded", r.error
    assert [x["double"] for x in r.outputs["a"]] == [i * 2 for i in range(10)]
    assert sorted(Batcher.calls) == [2, 4, 4]
    assert r.spend["a"]["llm_calls"] == 3
    assert (r.input_tokens, r.output_tokens, r.cache_read_tokens) == (3000, 300, 2400)
    assert r.cost_usd == pytest.approx((600 * 3 + 2400 * 0.3 + 300 * 15) / 1e6)


def test_bad_or_missing_items_fall_back_to_one_call_each(tmp_path):
    Batcher.broken, Batcher.missing = {1}, {2}
    with Store(tmp_path / "db") as store:
        r = Runner(pipeline(per_call=5, n=5), store=store, use_cache=False).run()
    assert [x["double"] for x in r.outputs["a"]] == [0, 2, 4, 6, 8]
    assert sorted(Batcher.calls) == [1, 1, 5]  # one batch, then records 1 and 2 alone
    assert r.spend["a"]["llm_calls"] == 3


def test_cache_is_shared_between_batch_sizes(tmp_path):
    with Store(tmp_path / "db") as store:
        Runner(pipeline(per_call=1, n=6), store=store).run()
        Batcher.calls = []
        r = Runner(pipeline(per_call=3, n=6), store=store).run()
    assert Batcher.calls == [] and r.llm_calls == 0
    assert [x["double"] for x in r.outputs["a"]] == [0, 2, 4, 6, 8, 10]


def test_records_per_call_is_bounded():
    with pytest.raises(ValueError):
        pipeline(per_call=0)


def test_anthropic_marks_the_instruction_cacheable(monkeypatch):
    sent = {}

    class Messages:
        def parse(self, **kw):
            sent.update(kw)
            usage = types.SimpleNamespace(
                input_tokens=20, output_tokens=5, cache_read_input_tokens=1500, cache_creation_input_tokens=0
            )
            return types.SimpleNamespace(
                stop_reason="end_turn", usage=usage, parsed_output=kw["output_format"](ok=True)
            )

    fake = types.ModuleType("anthropic")
    fake.Anthropic = lambda: types.SimpleNamespace(messages=Messages())  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    prov = provider(LLMSpec(options={"temperature": 0}))
    out = prov.structured("Check it.", {"x": 1}, schema_model("o", {"ok": "bool"}))
    head, body = sent["messages"][0]["content"]
    assert head == {"type": "text", "text": "Instruction: Check it.", "cache_control": {"type": "ephemeral"}}
    assert body["text"].startswith("<record>") and "cache_control" not in body
    assert "prompt_cache" not in sent and sent["temperature"] == 0
    assert (out.input_tokens, out.cache_read_tokens) == (1520, 1500)

    prov = provider(LLMSpec(options={"prompt_cache": False}))
    prov.structured("Check it.", {"x": 1}, schema_model("o", {"ok": "bool"}))
    assert "cache_control" not in sent["messages"][0]["content"][0]


def test_batch_schema_is_strict_for_the_provider():
    from agentl.llm import _strict_schema
    from agentl.runner import _batch_schema

    js = _strict_schema(_batch_schema("a", schema_model("o", {"double": "int", "note": "str?"})))
    item = js["properties"]["items"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == {"double", "note", "index"}
