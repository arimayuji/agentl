import json
from typing import ClassVar

import pytest

from agentl import connector, parse, register_provider
from agentl.cli import main
from agentl.llm import Result
from agentl.registry import get
from agentl.resolve import normalise, resolve
from agentl.runner import Runner
from agentl.store import Store

REGIONS = [
    {"id": "SP", "name": "Sao Paulo"},
    {"id": "RJ", "name": "Rio de Janeiro"},
    {"id": "MG", "name": "Minas Gerais"},
]


class Fake:
    """Picks the first candidate whose name shares a word with the value; counts calls."""

    calls: ClassVar[list] = []

    def __init__(self, spec):
        pass

    def structured(self, instruction, record, schema):
        Fake.calls.append(record["value"])
        words = set(normalise(record["value"]).split())
        pick = next(
            (c["id"] for c in record["candidates"] if words & set(normalise(c["name"]).split())), None
        )
        return Result(schema.model_validate({"match": pick}), 5, 1)

    def text(self, instruction, content):
        return ""


@connector("test.resolve_rows")
def _rows(input, names=()):
    return [{"region": n} for n in names]


def pipeline(names, **opts):
    return parse(
        {
            "name": "resolve-test",
            "llm": {"provider": "fake-resolve"},
            "steps": [
                {"id": "src", "use": "test.resolve_rows", "with": {"names": names}},
                {
                    "id": "match",
                    "use": "resolve",
                    "input": "src",
                    "with": {"field": "region", "reference": {"values": REGIONS}, **opts},
                },
            ],
        }
    )


@pytest.fixture
def store(tmp_path):
    register_provider("fake-resolve", Fake)
    Fake.calls = []
    with Store(tmp_path / "r.db") as s:
        yield s


def test_normalise_drops_case_accents_punctuation_and_suffixes():
    assert normalise("  São-Paulo, LTDA. ") == "sao paulo"
    assert normalise("Acme Inc.") == "acme"
    assert normalise("Inc") == "inc"  # never empties a name


def test_exact_and_fuzzy_without_a_model():
    rows = [{"region": "SÃO PAULO"}, {"region": "sp"}, {"region": "Rio de Janiero"}, {"region": "Mars"}]
    out, counts, _ = resolve(rows, REGIONS, "region", threshold=0.85)
    assert [r["region_match"] for r in out] == ["SP", "SP", "RJ"]
    assert [r["region_match_method"] for r in out] == ["exact", "exact", "fuzzy"]
    assert out.rejected[0]["region_match"] is None and "_error" in out.rejected[0]
    assert counts == {"exact": 2, "fuzzy": 1, "model": 0, "unresolved": 1}


def test_on_unresolved_keep_and_drop():
    rows = [{"region": "Mars"}]
    kept, _, _ = resolve(rows, REGIONS, "region", on_unresolved="keep")
    assert kept[0]["region_match_method"] == "unresolved" and not kept.rejected
    dropped, _, _ = resolve(rows, REGIONS, "region", on_unresolved="drop")
    assert list(dropped) == [] and not dropped.rejected


def test_connector_called_directly():
    out = get("resolve")([{"region": "Minas Gerais"}], field="region", reference={"values": REGIONS})
    assert out[0]["region_match"] == "MG"


def test_clean_names_make_zero_model_calls(store):
    res = Runner(pipeline(["Sao Paulo", "rio de janeiro", "MG"]), store=store).run()
    assert res.status == "succeeded" and Fake.calls == []
    assert [r["region_match"] for r in res.outputs["match"]] == ["SP", "RJ", "MG"]
    (event,) = [e for e in store.events(res.run_id) if e["type"] == "resolve.counts"]
    data = event["data"] if isinstance(event["data"], dict) else json.loads(event["data"])
    assert data == {"exact": 3, "fuzzy": 0, "model": 0, "unresolved": 0, "cached": 0}


def test_ambiguous_go_to_the_model_once_and_unresolved_are_rejected(store):
    names = ["Paulo state", "Paulo state", "Gerais region", "Atlantis"]
    first = Runner(pipeline(names, min_score=0.2), store=store).run()
    assert Fake.calls == ["Paulo state", "Gerais region", "Atlantis"]  # one call per distinct value
    assert [r["region_match"] for r in first.outputs["match"]] == ["SP", "SP", "MG"]
    assert {r["region_match_method"] for r in first.outputs["match"]} == {"model"}
    assert [r["region"] for r in first.rejected["match"]] == ["Atlantis"]
    assert first.llm_calls == 3
    again = Runner(pipeline(names, min_score=0.2), store=store).run()
    assert len(Fake.calls) == 3 and again.llm_calls == 0  # decisions are stored
    (event,) = [e for e in store.events(again.run_id) if e["type"] == "resolve.counts"]
    data = event["data"] if isinstance(event["data"], dict) else json.loads(event["data"])
    assert data["cached"] == 3 and data["model"] == 3 and data["unresolved"] == 1


def test_decisions_export_and_import(store, tmp_path, capsys):
    Runner(pipeline(["Paulo state"], min_score=0.2), store=store).run()
    db = str(tmp_path / "r.db")
    out = tmp_path / "d.json"
    assert main(["resolve", "export", "resolve-test", "match", str(out), "--db", db]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == {"paulo state": "SP"}
    out.write_text(json.dumps({"paulo state": "RJ"}), encoding="utf-8")  # a reviewer's correction
    assert main(["resolve", "import", "resolve-test", "match", str(out), "--db", db]) == 0
    res = Runner(pipeline(["Paulo state"], min_score=0.2), store=store).run()
    assert res.outputs["match"][0]["region_match"] == "RJ" and len(Fake.calls) == 1


def test_model_answer_outside_the_candidates_is_ignored(store):
    class Liar(Fake):
        def structured(self, instruction, record, schema):
            return Result(schema.model_validate({"match": "XX"}))

    register_provider("fake-resolve", Liar)
    res = Runner(pipeline(["Paulo state"], min_score=0.2), store=store).run()
    assert res.outputs["match"] == [] and res.rejected["match"][0]["region_match"] is None


@connector("test.resolve_regions")
def _regions(input):
    return REGIONS


def test_reference_from_a_previous_step(store):
    spec = parse(
        {
            "name": "resolve-step",
            "steps": [
                {"id": "regions", "use": "test.resolve_regions"},
                {"id": "src", "use": "test.resolve_rows", "with": {"names": ["minas gerais"]}},
                {
                    "id": "match",
                    "use": "resolve",
                    "input": ["src", "regions"],
                    "with": {"field": "region", "reference": "regions", "model": False},
                },
            ],
        }
    )
    res = Runner(spec, store=store).run()
    assert [r["region_match"] for r in res.outputs["match"]] == ["MG"]


# ---------------------------------------------------------------- fuzzy backends
def _block_rapidfuzz(monkeypatch):
    import builtins

    real = builtins.__import__

    def fake(name, *args, **kwargs):
        if name == "rapidfuzz" or name.startswith("rapidfuzz."):
            raise ImportError("blocked for the test")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake)


def test_difflib_backend_without_rapidfuzz(monkeypatch):
    import difflib

    from agentl import resolve as mod

    _block_rapidfuzz(monkeypatch)
    score = mod._scorer()
    assert score("rio de janiero", "rio de janeiro") == pytest.approx(
        difflib.SequenceMatcher(None, "rio de janiero", "rio de janeiro").ratio()
    )
    out, counts, _ = resolve([{"region": "Rio de Janiero"}], REGIONS, "region", threshold=0.85)
    assert out[0]["region_match"] == "RJ" and counts["fuzzy"] == 1


def test_rapidfuzz_backend_is_used_when_installed(monkeypatch):
    import sys
    import types

    from agentl import resolve as mod

    calls = []
    fuzz = types.SimpleNamespace(ratio=lambda a, b: calls.append((a, b)) or 50.0)
    pkg = types.ModuleType("rapidfuzz")
    pkg.fuzz = fuzz  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rapidfuzz", pkg)
    assert mod._scorer()("a", "b") == 0.5 and calls == [("a", "b")]


def test_real_rapidfuzz_agrees_on_the_match():
    pytest.importorskip("rapidfuzz")
    out, counts, _ = resolve([{"region": "Rio de Janiero"}], REGIONS, "region", threshold=0.85)
    assert out[0]["region_match"] == "RJ" and counts["fuzzy"] == 1
