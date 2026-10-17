"""Schema contracts on any step (#122)."""

import csv
import json

import pytest

from agentl import connector, parse
from agentl.contracts import ContractError, validate
from agentl.registry import stream_reader
from agentl.runner import Runner
from agentl.spec import SpecError
from agentl.store import Store

ROWS = [
    {"id": 1, "total": 10.5, "status": "paid", "email": "a@example.com"},
    {"id": "2", "total": "R$ 1.234,56", "status": "pending"},
    {"id": "x", "total": 3, "status": "paid"},
    {"id": 4, "total": 7, "status": "lost", "email": None},
    {"total": 1, "status": "paid"},
]
SCHEMA = {"id": "int", "total": {"money": "BRL"}, "status": {"enum": ["paid", "pending"]}, "email": "email?"}


@connector("test.contract_rows")
def _rows(input=None):
    return [dict(r) for r in ROWS]


@stream_reader("test.contract_rows")
def _rows_chunks(input=None, chunk_size=1):
    for i in range(0, len(ROWS), chunk_size):
        yield [dict(r) for r in ROWS[i : i + chunk_size]]


def run(tmp_path, steps, **top):
    spec = parse({"name": "contracts", "steps": steps, **top})
    with Store(tmp_path / "state.db") as store:
        r = Runner(spec, store=store).run()
        quarantined = store.rejected_list("contracts", "orders", pending=True)
    return r, quarantined


def src(**contract):
    return {"id": "orders", "use": "test.contract_rows", "schema": SCHEMA, **contract}


def test_reject_keeps_clean_rows_and_rejects_the_rest_with_reasons(tmp_path):
    r, quarantined = run(tmp_path, [src()])
    assert r.status == "succeeded", r.error
    assert r.outputs["orders"] == [ROWS[0]]
    bad = r.rejected["orders"]
    assert [b.get("id") for b in bad] == ["2", "x", 4, None]
    assert "id: '2' is not a clean int" in bad[0]["_error"]
    assert "total" in bad[0]["_error"]
    assert "id:" in bad[1]["_error"]
    assert "status:" in bad[2]["_error"] and "email" not in bad[2]["_error"]
    assert bad[3]["_error"].startswith("id: required")
    assert bad[0]["_raw"] == {"id": "2", "total": "R$ 1.234,56"}
    assert len(quarantined) == 4


def test_coerce_converts_what_it_can(tmp_path):
    r, _ = run(tmp_path, [src(on_bad="coerce")])
    assert r.status == "succeeded", r.error
    assert r.outputs["orders"][1] == {"id": 2, "total": 1234.56, "status": "pending"}
    assert len(r.outputs["orders"]) == 2  # 'x' is not an int, 'lost' is not in the enum, no id
    assert {b["_error"].split(":")[0] for b in r.rejected["orders"]} == {"id", "status"}


def test_fail_stops_the_run_naming_record_and_field(tmp_path):
    r, _ = run(tmp_path, [src(on_bad="fail")])
    assert r.status == "failed"
    assert "step 'orders'" in r.error and "record 1" in r.error and "id:" in r.error


def test_validate_fail_raises():
    with pytest.raises(ContractError, match="record 0: n:"):
        validate([{"n": "a"}], {"n": "int"}, on_bad="fail")


@pytest.mark.parametrize(
    ("extra", "out"),
    [
        ("keep", [{"id": 1, "note": "x"}]),
        ("drop", [{"id": 1}]),
        ("forbid", []),
    ],
)
def test_extra_modes(extra, out):
    good, bad = validate([{"id": 1, "note": "x"}], {"id": "int"}, extra=extra)
    assert good == out
    if extra == "forbid":
        assert bad[0]["_error"] == "note: not in the schema"


def test_schema_on_a_fn_like_transform_and_rejected_to(tmp_path):
    path = tmp_path / "bad.json"
    steps = [
        {"id": "raw", "use": "test.contract_rows"},
        {
            "id": "orders",
            "use": "select",
            "input": "raw",
            "with": {"fields": ["id", "status"]},
            "schema": {"id": "int", "status": "text"},
            "rejected_to": {"use": "json.write", "with": {"path": str(path)}},
        },
    ]
    r, _ = run(tmp_path, steps)
    assert r.status == "succeeded", r.error
    written = json.loads(path.read_text())
    assert [w.get("id") for w in written] == ["2", "x", None]
    assert all("_error" in w for w in written)


def test_check_on_rejected_rate(tmp_path):
    steps = [src(), {"id": "gate", "check": "orders", "expect": {"rejected_rate": "< 1%"}}]
    r, _ = run(tmp_path, steps)
    assert r.status == "failed"
    assert "4 of 5 records rejected (80.00%)" in r.error
    steps[1]["expect"] = {"rejected_rate": {"orders": "<= 80%"}}
    r, _ = run(tmp_path, steps)
    assert r.status == "succeeded", r.error


def test_item_rejected_event_is_emitted(tmp_path):
    seen = []
    spec = parse({"name": "contracts", "steps": [src()]})
    with Store(tmp_path / "s.db") as store:
        Runner(spec, store=store, listeners=[seen.append]).run()
    ev = [e for e in seen if e["type"] == "item.rejected"]
    assert ev and ev[0]["data"]["count"] == 4 and ev[0]["data"]["total"] == 5


def test_chunked_chain_validates_per_chunk(tmp_path):
    out = tmp_path / "out.json"
    steps = [
        src(),
        {"id": "keep", "use": "select", "input": "orders", "with": {"fields": ["id"]}},
        {"id": "save", "use": "json.write", "input": "keep", "with": {"path": str(out)}},
    ]
    r, _ = run(tmp_path, steps, chunk_size=2)
    assert r.status == "succeeded", r.error
    assert json.loads(out.read_text()) == [{"id": 1}]
    assert len(r.rejected["orders"]) == 4


def test_chunked_fail_names_the_record_across_chunks(tmp_path):
    keep = {"id": "keep", "use": "select", "input": "orders", "with": {"fields": ["id"]}}
    head = {**src(on_bad="fail"), "schema": {"status": {"enum": ["paid", "pending"]}}}
    seen = []
    spec = parse({"name": "contracts", "chunk_size": 2, "steps": [head, keep]})
    with Store(tmp_path / "s.db") as store:
        r = Runner(spec, store=store, listeners=[seen.append]).run()
    assert r.status == "failed" and "record 3: status" in r.error
    assert any(e["type"] == "step.started" and e["data"].get("chunk_size") == 2 for e in seen)


def test_arrow_input_falls_back_to_records(tmp_path):
    pytest.importorskip("pyarrow")
    path = tmp_path / "in.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "amount"])
        w.writerows([[1, "2.5"], [2, "abc"], [3, "4"]])
    out = tmp_path / "out.parquet"
    steps = [
        {
            "id": "orders",
            "use": "csv.read",
            "with": {"path": str(path)},
            "schema": {"id": "int", "amount": {"type": "decimal", "locale": "en-US"}},
            "on_bad": "coerce",
        },
        {"id": "save", "use": "parquet.write", "input": "orders", "with": {"path": str(out)}},
    ]
    r, _ = run(tmp_path, steps, arrow=True)
    assert r.status == "succeeded", r.error
    assert [b["id"] for b in r.rejected["orders"]] in ([2], ["2"])
    import pyarrow.parquet as pq

    assert pq.read_table(out).to_pylist() == [{"id": 1, "amount": 2.5}, {"id": 3, "amount": 4.0}]


def test_agent_steps_keep_output_and_take_schema(tmp_path):
    steps = [
        {"id": "raw", "use": "test.contract_rows"},
        {
            "id": "orders",
            "agent": "echo it",
            "input": "raw",
            "output": {"id": "str"},
            "schema": {"id": "int"},
        },
    ]
    spec = parse({"name": "contracts", "llm": {"provider": "echo"}, "steps": steps})
    assert spec.step("orders").output == {"id": "str"}


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ({"schema": {"id": "integr"}}, "unknown type 'integr'; did you mean 'int'"),
        ({"schema": {}}, "map each field to a type"),
        ({"on_bad": "coerce"}, "on_bad goes with a 'schema'"),
        ({"extra": "drop"}, "extra goes with a 'schema'"),
        ({"schema": {"id": "int"}, "on_bad": "keep"}, "on_bad"),
    ],
)
def test_parse_time_errors(step, message):
    with pytest.raises(SpecError, match=message):
        parse({"name": "x", "steps": [{"id": "s", "use": "test.contract_rows", **step}]})


def test_rejected_rate_parse_errors():
    base = {"id": "s", "use": "test.contract_rows"}
    with pytest.raises(SpecError, match="rejected_rate 'lots'"):
        parse({"name": "x", "steps": [base, {"id": "c", "check": "s", "expect": {"rejected_rate": "lots"}}]})
    with pytest.raises(SpecError, match="not upstream"):
        parse(
            {
                "name": "x",
                "steps": [
                    base,
                    {"id": "t", "use": "test.contract_rows"},
                    {"id": "c", "check": "s", "expect": {"rejected_rate": {"t": "1%"}}},
                ],
            }
        )


def test_optional_money_keeps_its_currency():
    good, _ = validate([{"v": "R$ 2,00"}, {"v": None}], {"v": {"money?": "BRL"}}, on_bad="coerce")
    assert good == [{"v": 2.0}, {"v": None}]
