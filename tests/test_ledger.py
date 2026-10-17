import json

import pytest

from agentl import ledger, parse, register_provider
from agentl.cli import main
from agentl.llm import Result
from agentl.runner import Runner
from agentl.store import Store


class Priced:
    """Fake provider: every call uses 100 input and 10 output tokens."""

    def __init__(self, spec):
        pass

    def structured(self, instruction, record, schema):
        return Result(schema.model_validate({"ok": True}), 100, 10)

    def text(self, instruction, content):
        return ""


register_provider("ledger-fake", Priced)


def rows(input=None, n=3):
    return [{"i": i} for i in range(n)]


def upsert(input):
    return [{"inserted": 2, "updated": 1, "unchanged": 0}]


def spec():
    llm = {"provider": "ledger-fake", "model": "small", "price": {"input": 1.0, "output": 2.0}}
    return parse(
        {
            "name": "facts",
            "llm": {"default": llm},
            "steps": [
                {"id": "src", "fn": "tests.test_ledger:rows"},
                {"id": "ask", "agent": "Check.", "input": "src", "output": {"ok": "bool"}},
                {"id": "save", "fn": "tests.test_ledger:upsert", "input": "ask"},
            ],
        }
    )


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "db.sqlite"
    with Store(path) as store:
        result = Runner(spec(), store=store, use_cache=False).run()
    return path, result.run_id


def test_show_reports_rows_changes_and_spend(db):
    path, run_id = db
    with Store(path) as store:
        report = ledger.show(store, run_id)
        assert ledger.show(store, "missing") is None
    assert report["status"] == "succeeded"
    assert report["changed"] == 3
    assert report["llm_calls"] == 3
    assert report["input_tokens"] == 300 and report["output_tokens"] == 30
    steps = {s["step"]: s for s in report["steps"]}
    assert steps["src"]["records"] == 3
    assert steps["ask"]["models"]["small"]["llm_calls"] == 3
    assert steps["save"]["inserted"] == 2 and steps["save"]["updated"] == 1
    assert "changed 3 rows" in ledger.show_text(report)


def test_cost_groups_by_pipeline_and_model(db):
    path, _ = db
    with Store(path) as store:
        report = ledger.cost(store, 0, ["pipeline", "model"])
        by_step = ledger.cost(store, 0, ["step"])
        with pytest.raises(ValueError):
            ledger.cost(store, 0, ["colour"])
    (g,) = report["groups"]
    assert (g["pipeline"], g["model"], g["llm_calls"], g["runs"]) == ("facts", "small", 3, 1)
    assert g["cost_usd"] == pytest.approx(report["total_usd"])
    assert g["cost_usd"] > 0
    assert [x["step"] for x in by_step["groups"]] == ["ask"]


def test_cost_counts_old_events_without_models(tmp_path):
    with Store(tmp_path / "db.sqlite") as store:
        store.start_run("r1", "old", "")
        store.event("r1", "step.succeeded", "ask", {"llm_calls": 2, "input_tokens": 5, "cost_usd": 0.5})
        report = ledger.cost(store, 0, ["model"])
    assert report["groups"][0]["model"] == "?"
    assert report["total_usd"] == 0.5


def test_cli_runs_show_and_cost(db, capsys):
    path, run_id = db
    assert main(["runs", "show", run_id, "--db", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == 3
    assert main(["runs", "show", "nope", "--db", str(path)]) == 2
    assert main(["cost", "--db", str(path), "--by", "pipeline,model"]) == 0
    out = capsys.readouterr().out
    assert "facts  small" in out and "total $" in out
    assert main(["runs", "--db", str(path)]) == 0
    assert run_id in capsys.readouterr().out
