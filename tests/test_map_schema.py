"""map_schema: infer a column mapping once, keep it per layout, apply it with no model call (#177)."""

import pytest

from agentl import parse, register_provider
from agentl.cli import main
from agentl.llm import Result
from agentl.mapping import header, map_schema, match, normalise
from agentl.runner import Runner
from agentl.store import Store

CALLS: list[dict] = []


class FakeMapper:
    """Maps `value` to the 'Sales / Units' column; answers null for anything else."""

    def __init__(self, spec):
        pass

    def structured(self, instruction, record, schema):
        CALLS.append(record)
        answer = {f: ("Sales / Units" if f == "value" else None) for f in schema.model_fields}
        return Result(schema.model_validate(answer), 100, 10)

    def text(self, instruction, content):
        return ""


register_provider("map-fake", FakeMapper)

# A report with a two-row header: the reader's header row, plus one more row with a merged cell.
SHEET = [
    {"Report date": "", "Região": "", "Sales": "Units", "Unnamed: 3": "Revenue"},
    {"Report date": "02/01/2026", "Região": "North", "Sales": "1.200", "Unnamed: 3": "10,5"},
    {"Report date": "", "Região": "", "Sales": "", "Unnamed: 3": ""},
    {"Report date": "03/01/2026", "Região": "South", "Sales": "300", "Unnamed: 3": "2,0"},
]
OTHER = [{"date": "02/01/2026", "area": "North", "qty": "5"}]
DATA = {"rows": SHEET}


def rows(input=None):
    return [dict(r) for r in DATA["rows"]]


def pipeline(**with_):
    target = {"report_date": "date", "region": "str", "value": "float"}
    return parse(
        {
            "name": "sales",
            "llm": {"default": {"provider": "map-fake", "price": {"input": 1, "output": 1}}},
            "steps": [
                {"id": "sheet", "fn": "tests.test_map_schema:rows"},
                {
                    "id": "mapped",
                    "use": "map_schema",
                    "input": "sheet",
                    "with": {
                        "target": target,
                        "header_rows": 1,
                        "synonyms": {"region": ["regiao"]},
                        **with_,
                    },
                },
            ],
        }
    )


@pytest.fixture(autouse=True)
def reset():
    CALLS.clear()
    DATA["rows"] = SHEET
    yield


def test_header_joins_rows_and_fills_merged_cells():
    names, data = header(SHEET, header_rows=1)
    assert names == ["Report date", "Região", "Sales / Units", "Sales / Revenue"]
    assert data[0]["Sales / Units"] == "1.200"
    assert normalise("Região (UF)") == "regiao_uf"


def test_match_by_name_and_synonyms():
    names, _ = header(SHEET, header_rows=1)
    found = match(names, {"report_date": "date", "region": "str", "units": "int"}, {"region": "regiao"})
    assert found == {"report_date": "Report date", "region": "Região", "units": "Sales / Units"}


def test_second_run_on_same_layout_makes_no_model_call(tmp_path):
    store = Store(tmp_path / "s.db")
    first = Runner(pipeline(), store=store).run()
    assert first.status == "succeeded", first.error
    assert len(CALLS) == 1 and first.llm_calls == 1
    assert CALLS[0]["already_mapped"] == {"report_date": "Report date", "region": "Região"}
    assert first.outputs["mapped"] == [
        {"report_date": "2026-01-02", "region": "North", "value": 1200.0},
        {"report_date": "2026-01-03", "region": "South", "value": 300.0},
    ]

    second = Runner(pipeline(), store=store).run()
    assert second.status == "succeeded"
    assert len(CALLS) == 1 and second.llm_calls == 0
    assert second.outputs["mapped"] == first.outputs["mapped"]


def test_no_llm_call_when_names_match(tmp_path):
    DATA["rows"] = [{"report_date": "02/01/2026", "region": "North", "value": "1"}]
    result = Runner(pipeline(header_rows=0), store=Store(tmp_path / "s.db")).run()
    assert result.status == "succeeded" and CALLS == []


def test_new_layout_fails_clearly(tmp_path):
    store = Store(tmp_path / "s.db")
    assert Runner(pipeline(), store=store).run().status == "succeeded"
    DATA["rows"] = OTHER
    result = Runner(pipeline(header_rows=0), store=store).run()
    assert result.status == "failed"
    assert "layout changed" in result.error and "new columns" in result.error
    assert len(CALLS) == 1  # never inferred silently

    again = Runner(pipeline(header_rows=0, on_drift="infer"), store=store).run()
    assert again.status == "succeeded" and len(CALLS) == 2


def test_review_required_waits_for_approval(tmp_path, capsys):
    db = tmp_path / "s.db"
    result = Runner(pipeline(review="required"), store=Store(db)).run()
    assert result.status == "failed" and "waits for review" in result.error

    assert main(["mappings", "show", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "sales  mapped" in out and "waiting for review" in out and 'value: "Sales / Units"' in out
    assert main(["mappings", "approve", "sales", "mapped", "--db", str(db)]) == 0

    ok = Runner(pipeline(review="required"), store=Store(db)).run()
    assert ok.status == "succeeded" and len(CALLS) == 1

    assert main(["mappings", "forget", "sales", "mapped", "--db", str(db)]) == 0
    assert main(["mappings", "approve", "sales", "mapped", "--db", str(db)]) == 1


def test_pinned_mapping_skips_inference(tmp_path):
    pinned = {"report_date": "Report date", "region": "Região", "value": "Sales / Revenue"}
    result = Runner(pipeline(mapping=pinned), store=Store(tmp_path / "s.db")).run()
    assert result.status == "succeeded" and CALLS == []
    assert [r["value"] for r in result.outputs["mapped"]] == [10.5, 2.0]

    bad = Runner(pipeline(mapping={"value": "Nope"}), store=Store(tmp_path / "s.db")).run()
    assert bad.status == "failed" and "Nope" in bad.error


def test_connector_outside_a_run_maps_by_name():
    out = map_schema([{"Report Date": "2026-01-02", "x": 1}], target={"report_date": "date", "x": "int"})
    assert out == [{"report_date": "2026-01-02", "x": 1}]
