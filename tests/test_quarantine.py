import json
from typing import ClassVar

import pytest

from agentl import connector, parse, register_provider
from agentl.cli import main
from agentl.llm import Result
from agentl.runner import Runner, retry_rejected
from agentl.store import Store


class Picky:
    """Rejects records with bad=True until `fixed`; counts calls per instruction."""

    fixed = False
    calls: ClassVar[list] = []

    def __init__(self, spec):
        pass

    def structured(self, instruction, record, schema):
        Picky.calls.append((instruction, record["n"]))
        if record.get("bad") and not Picky.fixed and instruction == "label":
            raise ValueError("invalid output")
        field = next(iter(schema.model_fields))
        return Result(schema.model_validate({field: f"{instruction}{record['n']}"}), 10, 2)


@connector("test.quarantine_rows")
def _rows(input):
    return [{"n": 1}, {"n": 2}, {"n": 3, "bad": True}]


RAW = {
    "name": "quarantine-test",
    "llm": {"provider": "picky"},
    "steps": [
        {"id": "src", "use": "test.quarantine_rows"},
        {"id": "label", "agent": "label", "input": "src", "output": {"label": "str"}},
        {"id": "tag", "agent": "tag", "input": "label", "output": {"tag": "str"}},
        {"id": "other", "use": "select", "input": "src", "with": {"fields": ["n"]}},
    ],
}


@pytest.fixture
def store(tmp_path):
    register_provider("picky", Picky)
    Picky.fixed, Picky.calls = False, []
    with Store(tmp_path / "q.db") as s:
        yield s


def test_rejected_records_are_kept_and_retried_downstream(store):
    spec = parse(RAW)
    first = Runner(spec, store=store).run()
    assert first.status == "succeeded" and len(first.outputs["tag"]) == 2
    rows = store.rejected_list("quarantine-test")
    assert len(rows) == 1
    row = rows[0]
    assert row["step"] == "label" and row["run_id"] == first.run_id
    assert row["error"] == "invalid output"
    assert row["record"] == {"n": 3, "bad": True, "_error": "invalid output"}
    assert row["retried_by"] is None

    Picky.fixed, Picky.calls = True, []
    again = retry_rejected(spec, store)
    assert again is not None and again.status == "succeeded", again.error
    # only the rejected record runs, through the step and what is downstream of it
    assert Picky.calls == [("label", 3), ("tag", 3)]
    assert again.outputs["tag"] == [{"n": 3, "bad": True, "label": "label3", "tag": "tag3"}]
    assert "other" not in again.outputs and "src" not in again.outputs
    assert store.rejected_get(row["id"])["retried_by"] == again.run_id
    assert store.rejected_list("quarantine-test", pending=True) == []
    assert retry_rejected(spec, store) is None  # nothing left

    # a normal run afterwards pays nothing: all three records are cached now
    Picky.calls = []
    assert Runner(spec, store=store).run().status == "succeeded"
    assert Picky.calls == []


def test_rejected_again_stays_in_quarantine(store):
    spec = parse(RAW)
    Runner(spec, store=store).run()
    again = retry_rejected(spec, store, step="label")
    assert again is not None and again.status == "succeeded"
    pending = store.rejected_list("quarantine-test", "label", pending=True)
    assert [r["run_id"] for r in pending] == [again.run_id]
    assert store.rejected_list("quarantine-test", pending=False)[0]["retried_by"] == again.run_id


def test_cast_rejects_are_kept_masked_and_can_be_turned_off(store, monkeypatch):
    @connector("test.quarantine_cast_src")
    def src(input):
        return [{"n": "1"}, {"n": "x s3cr3t-value"}]

    raw = {
        "name": "cast-q",
        "steps": [
            {"id": "src", "use": "test.quarantine_cast_src"},
            {"id": "typed", "use": "cast", "input": "src", "with": {"fields": {"n": "int"}}},
        ],
    }
    monkeypatch.setattr("agentl.secrets.sensitive", lambda: {"s3cr3t-value"})
    Runner(parse(raw), store=store).run()
    (row,) = store.rejected_list("cast-q")
    assert row["step"] == "typed" and row["record"]["n"] == "x ***"
    assert "s3cr3t" not in json.dumps(row)

    monkeypatch.setenv("AGENTL_QUARANTINE", "0")
    result = Runner(parse(raw), store=store).run()
    assert len(result.rejected["typed"]) == 1
    assert len(store.rejected_list("cast-q")) == 1


def test_several_steps_need_a_step(store):
    spec = parse(RAW)
    Runner(spec, store=store).run()
    store.rejected_add("r0", "quarantine-test", "tag", [{"n": 9, "_error": "x"}])
    with pytest.raises(ValueError, match="--step"):
        retry_rejected(spec, store)


def test_cli_list_show_retry(store, tmp_path, capsys):
    import yaml

    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(RAW))
    db = str(store.path)
    assert main(["run", str(path), "--db", db, "--no-check"]) == 0
    capsys.readouterr()
    assert main(["rejected", "list", "--db", db, "--pipeline", "quarantine-test"]) == 0
    line = capsys.readouterr().out.strip()
    assert "quarantine-test  label" in line and "invalid output" in line
    rid = line.split()[0]
    assert main(["rejected", "show", rid, "--db", db]) == 0
    assert json.loads(capsys.readouterr().out)["record"]["n"] == 3
    assert main(["rejected", "show", "999", "--db", db]) == 1
    Picky.fixed = True
    assert main(["rejected", "retry", str(path), "--db", db, "--step", "label"]) == 0
    capsys.readouterr()
    assert main(["rejected", "list", "--db", db]) == 0
    assert "no rejected records" in capsys.readouterr().out
    assert main(["rejected", "list", "--db", db, "--all"]) == 0
    assert "retried by" in capsys.readouterr().out
