"""Semantic dedupe for rewritten text (#200). Offline only: shingles, or a fake embedding provider."""

import pytest

from agentl import parse, register_provider, semantic
from agentl.connectors.transforms import dedupe
from agentl.llm import Embeddings
from agentl.runner import Runner
from agentl.store import Store

NEWS = [
    {"id": "a", "at": "2026-05-02T10:00:00Z", "text": "City council approves new budget for public parks"},
    {
        "id": "b",
        "at": "2026-05-01T09:00:00Z",
        "text": "City council approves the new budget for public parks!",
    },
    {"id": "c", "at": "2026-05-01T12:00:00Z", "text": "Storm closes the airport for six hours"},
    {"id": "d", "at": "2026-05-09T10:00:00Z", "text": "City Council approves new budget for public parks"},
]


def test_shingles_drop_near_duplicates_offline():
    out = dedupe(NEWS[:3], semantic={"field": "text"})
    assert [r["id"] for r in out] == ["a", "c"]


def test_keep_earliest_inside_a_window_and_report_why():
    kept, dropped, report = semantic.dedupe(
        NEWS, {"field": "text", "time": "at", "window": "48h", "keep": "earliest", "id": "id"}
    )
    # b is the earliest; a is its duplicate; d is a week later, outside the window
    assert [r["id"] for r in kept] == ["b", "c", "d"]
    assert dropped == [
        {
            **NEWS[0],
            "_duplicate_of": "b",
            "_similarity": dropped[0]["_similarity"],
            "_kept_because": "earliest at",
        }
    ]
    assert dropped[0]["_similarity"] >= 0.7
    assert report["kept"] == 3 and report["dropped"] == 1 and report["backend"] == "shingles"
    assert report["groups"][0]["kept"] == "b" and report["groups"][0]["duplicates"][0]["dropped"] == "a"


def test_mark_keeps_every_record_with_the_reference():
    out = dedupe(NEWS[:2], semantic={"field": "text", "mark": True})
    assert out[0]["_duplicate_of"] is None
    assert out[1]["_duplicate_of"] == 0 and out[1]["_kept_because"] == "first in input order"


def test_unrelated_texts_and_blank_texts_survive():
    rows = [{"text": ""}, {"text": None}, {"text": "one thing"}, {"text": "another matter"}]
    assert len(dedupe(rows, semantic={"field": "text"})) == 4


def test_exact_on_runs_first_and_options_are_checked():
    rows = [{"k": 1, "text": "x y z"}, {"k": 1, "text": "totally different words here"}]
    assert dedupe(rows, on="k", semantic={"field": "text"}) == [rows[0]]
    with pytest.raises(ValueError, match="need `time`"):
        dedupe(rows, semantic={"field": "text", "keep": "latest"})
    with pytest.raises(ValueError, match="unknown option"):
        dedupe(rows, semantic={"field": "text", "limit": 0.5})
    with pytest.raises(ValueError, match="inside a pipeline"):
        dedupe(rows, semantic={"field": "text", "llm": "emb"})
    with pytest.raises(ValueError, match="needs `on`"):
        dedupe(rows)


def test_minhash_is_deterministic():
    s = semantic.shingles("the same words in the same order")
    assert semantic.minhash(s) == semantic.minhash(set(s))


CALLS: list[list[str]] = []


class FakeEmbedder:
    """Paraphrases share a topic word; the vector is the topic, so rewrites match."""

    def __init__(self, spec):
        self.spec = spec

    def embed(self, texts):
        CALLS.append(list(texts))
        topics = ("budget", "storm")
        return Embeddings(
            [[1.0 if w in t.lower() else 0.0 for w in topics] for t in texts], input_tokens=10 * len(texts)
        )

    def structured(self, *a):
        raise AssertionError("not used")

    def text(self, *a):
        return ""


register_provider("embed-fake", FakeEmbedder)


def rows(input=None):
    return [
        {"id": 1, "text": "Council passes the parks budget"},
        {"id": 2, "text": "Parks get money: budget approved by council"},
        {"id": 3, "text": "Storm shuts the airport"},
    ]


def pipeline():
    return parse(
        {
            "name": "news",
            "llm": {"emb": {"provider": "embed-fake", "model": "m1", "price": {"input": 1, "output": 0}}},
            "steps": [
                {"id": "src", "fn": "tests.test_semantic_dedupe:rows"},
                {
                    "id": "unique",
                    "use": "dedupe",
                    "input": "src",
                    "with": {"semantic": {"field": "text", "llm": "emb", "id": "id", "threshold": 0.9}},
                },
            ],
        }
    )


def test_embeddings_are_cached_by_text_hash_and_charged(tmp_path):
    CALLS.clear()
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline(), store=store).run()
        assert [x["id"] for x in r.outputs["unique"]] == [1, 3]
        assert r.spend["unique"]["llm_calls"] == 1 and r.spend["unique"]["input_tokens"] == 30
        assert r.cost_usd == pytest.approx(30 / 1_000_000)
        ev = next(e for e in store.events(r.run_id) if e["type"] == "dedupe.semantic")
        assert ev["data"]["embedded"] == 3 and ev["data"]["groups"][0]["kept"] == 1
        r2 = Runner(pipeline(), store=store).run()
        assert len(CALLS) == 1  # second run: every vector from the cache
        assert r2.spend["unique"]["llm_calls"] == 0
        assert [x["id"] for x in r2.outputs["unique"]] == [1, 3]


def test_echo_provider_embeds_offline():
    from agentl.llm import EchoProvider
    from agentl.spec import LLMSpec

    v = EchoProvider(LLMSpec(provider="echo")).embed(["a b", "b a", "c"])
    assert v.vectors[0] == v.vectors[1] != v.vectors[2]


def test_openai_compatible_embed_parses_the_response(monkeypatch):
    import json

    from agentl.llm import OpenAICompatibleProvider
    from agentl.spec import LLMSpec

    p = OpenAICompatibleProvider(
        LLMSpec(
            provider="openai-compatible",
            model="chat",
            options={"base_url": "http://localhost:1", "embedding_model": "e5"},
        )
    )
    seen = {}

    def http(method, path, data=None, content_type=""):
        seen.update(path=path, body=json.loads(data))
        return json.dumps(
            {
                "data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}],
                "usage": {"prompt_tokens": 4},
            }
        ).encode()

    monkeypatch.setattr(p, "_http", http)
    out = p.embed(["x", "y"])
    assert seen == {"path": "/embeddings", "body": {"model": "e5", "input": ["x", "y"]}}
    assert out.vectors == [[1, 0], [0, 1]] and out.input_tokens == 4
    assert "embedding_model" not in p._payload([])
