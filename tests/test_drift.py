"""Layout drift (#178): signatures, the diff, and on_drift warn / fail / remap in a run."""

import datetime as dt

import pytest

from agentl import parse, register_channel
from agentl.drift import diff, signature, summary
from agentl.runner import Runner
from agentl.store import Store

pa = pytest.importorskip("pyarrow")

OLD = [{"id": 1, "name": "a", "total": 9.5, "day": dt.date(2026, 1, 1)}]


def test_signature_of_records_keeps_order_and_types():
    rows = [{"id": 1, "total": 2, "note": None}, {"id": 2, "total": 2.5, "note": "x", "extra": True}]
    assert signature(rows) == {
        "columns": ["id", "total", "note", "extra"],
        "types": {"id": "int", "total": "float", "note": "str", "extra": "bool"},
    }
    assert signature([{"a": 1}, {"a": "x"}, {"b": None}])["types"] == {"a": "int|str", "b": "null"}


def test_signature_of_arrow_matches_records():
    table = pa.Table.from_pylist(OLD)
    assert signature(table) == signature(OLD)


def test_same_layout_is_no_drift():
    assert diff(signature(OLD), signature([{**OLD[0], "id": 2}])) is None
    # a column empty in one run does not count as a type change
    assert diff(signature(OLD), signature([{**OLD[0], "name": None}])) is None


def test_diff_finds_added_removed_renamed_and_type_changes():
    new = [{"id": "1", "full_name": "a", "total": 9.5, "region": "south"}]
    d = diff(signature(OLD), signature(new))
    assert d == {
        "added": ["region"],
        "removed": ["day"],
        "renamed": [["name", "full_name"]],
        "type_changed": {"id": ["int", "str"]},
    }
    assert summary(d) == "added region; removed day; renamed name -> full_name; type of id int -> str"


def test_rename_by_position_and_on_arrow():
    old = pa.Table.from_pylist([{"a": 1, "qty": 2}])
    new = pa.Table.from_pylist([{"a": 1, "units": 2}])
    assert diff(signature(old), signature(new))["renamed"] == [["qty", "units"]]


def test_unrelated_columns_of_another_type_are_not_a_rename():
    d = diff(signature([{"a": 1, "qty": 2}]), signature([{"a": 1, "label": "x", "qty2": "y"}]))
    assert d["renamed"] == [] and d["removed"] == ["qty"]


# ---------------------------------------------------------------- runner
DATA: dict = {"rows": OLD}
SAVED: list = []
SENT: list = []

register_channel("drift-test", lambda cfg, text, event: SENT.append(event))


def src(input=None):
    return DATA["rows"]


def save(input):
    SAVED.append(len(input))
    return []


@pytest.fixture(autouse=True)
def reset():
    DATA["rows"] = OLD
    SAVED.clear()
    SENT.clear()


def pipeline(on_drift, source=None, tmp_path=None):
    first = {"id": "sheet", "fn": "tests.test_drift:src"}
    if source == "parquet":
        import pyarrow.parquet as pq

        path = tmp_path / "sheet.parquet"
        pq.write_table(pa.Table.from_pylist(DATA["rows"]), path)
        first = {"id": "sheet", "use": "parquet.read", "with": {"path": str(path)}}
    return parse(
        {
            "name": "sheet-import",
            "notify": {
                "channels": {"ops": {"type": "drift-test"}},
                "rules": [{"on": "schema.drift", "to": ["ops"]}],
            },
            "steps": [
                first,
                {
                    "id": "sheet_ok",
                    "check": {"schema_matches": "sheet", "on_drift": on_drift},
                    "input": "sheet",
                },
                {"id": "save", "fn": "tests.test_drift:save", "input": "sheet_ok"},
            ],
        }
    )


def run(tmp_path, spec):
    with Store(tmp_path / "s.db") as store:
        r = Runner(spec, store=store).run()
        return r, [e for e in store.events(r.run_id) if e["type"] == "schema.drift"]


NEW = [{"id": 1, "full_name": "a", "total": "9.5", "day": dt.date(2026, 1, 2), "region": "s"}]


@pytest.mark.parametrize("source", [None, "parquet"])
def test_fail_stops_and_notify_routes_the_event(tmp_path, source):
    r, events = run(tmp_path, pipeline("fail", source, tmp_path))
    assert r.status == "succeeded" and events == [] and SAVED == [1]
    DATA["rows"] = NEW
    spec = pipeline("fail", source, tmp_path)
    if source:
        with Store(tmp_path / "s.db") as store:
            assert "sheet" in Runner(spec, store=store)._arrow_plan({})  # the check reads a Table
    r, events = run(tmp_path, spec)
    assert r.status == "failed" and "changed layout" in r.error and SAVED == [1]
    data = events[0]["data"]
    assert data["added"] == ["region"] and data["renamed"] == [["name", "full_name"]]
    assert data["type_changed"] == {"total": ["float", "str"]} and data["on_drift"] == "fail"
    assert [e["type"] for e in SENT] == ["schema.drift"]
    r, _ = run(tmp_path, spec)  # still the old layout on record: still fails
    assert r.status == "failed"


def test_warn_passes_the_data_and_accepts_the_new_layout(tmp_path):
    spec = pipeline("warn")
    run(tmp_path, spec)
    DATA["rows"] = NEW
    r, events = run(tmp_path, spec)
    assert r.status == "succeeded" and len(events) == 1 and SAVED == [1, 1]
    r, events = run(tmp_path, spec)
    assert r.status == "succeeded" and events == []


def test_remap_stops_and_hands_over_both_layouts(tmp_path):
    spec = pipeline("remap")
    run(tmp_path, spec)
    DATA["rows"] = NEW
    r, events = run(tmp_path, spec)
    assert r.status == "failed" and "new reviewed mapping" in r.error
    data = events[0]["data"]
    assert data["previous"]["columns"] == ["id", "name", "total", "day"]
    assert data["current"]["columns"] == ["id", "full_name", "total", "day", "region"]


def test_drift_combines_with_expectations_and_spec_errors(tmp_path):
    spec = parse(
        {
            "name": "x",
            "steps": [
                {"id": "sheet", "fn": "tests.test_drift:src"},
                {"id": "c", "check": "sheet", "on_drift": "warn", "expect": {"rows": {"min": 2}}},
            ],
        }
    )
    r, _ = run(tmp_path, spec)
    assert r.status == "failed" and "expected at least 2" in r.error
    base = {"name": "x", "steps": [{"id": "sheet", "fn": "tests.test_drift:src"}]}
    with pytest.raises(ValueError, match="belongs to check steps"):
        parse({**base, "steps": [{"id": "s", "fn": "tests.test_drift:src", "on_drift": "warn"}]})
    with pytest.raises(ValueError, match="takes schema_matches"):
        parse({**base, "steps": [*base["steps"], {"id": "c", "check": {"step": "sheet"}}]})
