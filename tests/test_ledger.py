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


def _shared_host(store):
    """Two pipelines on one host: `a` runs 30 s (extract 10 s, ask 20 s on two models), `b` runs
    90 s (one step without LLM calls); `c` failed before any step finished."""
    rows = [("ra", "a", 0, 30), ("rb", "b", 100, 90), ("rc", "c", 300, 60)]
    for rid, name, start, secs in rows:
        store._exec(
            "INSERT INTO {runs} (id, pipeline, status, started, finished) VALUES (?, ?, ?, ?, ?)",
            (rid, name, "succeeded", start, start + secs),
        )
    store.event("ra", "step.succeeded", "extract", {"records": 4, "seconds": 10})
    models = {
        "big": {"llm_calls": 1, "input_tokens": 10, "output_tokens": 1, "cost_usd": 0.3},
        "small": {"llm_calls": 3, "input_tokens": 30, "output_tokens": 3, "cost_usd": 0.1},
    }
    store.event(
        "ra", "step.succeeded", "ask", {"llm_calls": 4, "cost_usd": 0.4, "seconds": 20, "models": models}
    )
    store.event("rb", "step.succeeded", "load", {"records": 9, "seconds": 90})


def test_cost_compute_hourly_by_pipeline_step_and_model(tmp_path):
    with Store(tmp_path / "db.sqlite") as store:
        _shared_host(store)
        price = ledger.compute_price(usd_per_hour=36)  # 0.01 USD per second
        by_pipe = ledger.cost(store, 0, ["pipeline"], compute=price)
        by_step = ledger.cost(store, 0, ["pipeline", "step", "model"], compute=price)
        plain = ledger.cost(store, 0, ["pipeline"])
    pipes = {g["pipeline"]: g for g in by_pipe["groups"]}
    assert pipes["a"]["compute_usd"] == pytest.approx(0.30)
    assert pipes["a"]["total_usd"] == pytest.approx(0.70)
    assert pipes["b"]["compute_usd"] == pytest.approx(0.90) and pipes["b"]["cost_usd"] == 0
    assert pipes["c"]["compute_usd"] == pytest.approx(0.60)
    assert by_pipe["compute_usd"] == pytest.approx(1.80) and by_pipe["llm_usd"] == pytest.approx(0.4)
    assert by_pipe["total_usd"] == pytest.approx(2.20)
    steps = {(g["pipeline"], g["step"], g["model"]): g for g in by_step["groups"]}
    assert steps[("a", "extract", "-")]["compute_usd"] == pytest.approx(0.10)
    assert steps[("a", "ask", "big")]["compute_usd"] == pytest.approx(0.05)
    assert steps[("a", "ask", "small")]["compute_usd"] == pytest.approx(0.15)
    assert steps[("c", "-", "-")]["compute_usd"] == pytest.approx(0.60)
    assert sum(g["compute_usd"] for g in by_step["groups"]) == pytest.approx(1.80)
    # without a compute price the report is LLM-only, as before
    assert [g["pipeline"] for g in plain["groups"]] == ["a"] and "compute" not in plain
    assert "compute $" in ledger.cost_text(by_pipe)


def test_cost_compute_lambda_and_shared_bill(tmp_path):
    with Store(tmp_path / "db.sqlite") as store:
        _shared_host(store)
        lam = ledger.cost(store, 0, ["pipeline"], compute=ledger.compute_price(lambda_memory_mb=2048))
        bill = ledger.cost(store, 0, ["pipeline"], compute=ledger.compute_price(bill_usd=18))
    a = next(g for g in lam["groups"] if g["pipeline"] == "a")
    assert a["compute_usd"] == pytest.approx(
        30 * 2 * ledger.LAMBDA_USD_PER_GB_SECOND + ledger.LAMBDA_USD_PER_REQUEST, abs=1e-6
    )
    split = {g["pipeline"]: g["compute_usd"] for g in bill["groups"]}
    assert split == pytest.approx({"a": 3.0, "b": 9.0, "c": 6.0})  # 30 / 90 / 60 of 180 s
    with pytest.raises(ValueError):
        ledger.compute_price(usd_per_hour=1, bill_usd=2)
    with pytest.raises(ValueError):
        ledger.compute_price(usd_per_hour=-1)
    assert ledger.compute_price() is None


def test_cli_cost_with_compute(tmp_path, capsys):
    path = tmp_path / "db.sqlite"
    with Store(path) as store:
        _shared_host(store)
    assert (
        main(
            [
                "cost",
                "--db",
                str(path),
                "--since",
                "100000d",
                "--by",
                "pipeline",
                "--usd-per-hour",
                "36",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["compute_usd"] == pytest.approx(1.80)
    assert main(["cost", "--db", str(path), "--usd-per-request", "1"]) == 2
