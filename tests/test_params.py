import importlib
import json
import sys
import types

import pytest

import agentl
from agentl import SpecError
from agentl.backfill import backfill, periods
from agentl.cli import main
from agentl.lint import check_text

PIPE = """name: daily
params:
  day: date
  limit: {type: int, default: 2}
  tag: {type: str, default: none}
steps:
  - {id: raw, use: csv.read, with: {path: '${params.src}'}}
  - id: out
    use: json.write
    input: raw
    with: {path: 'out/${params.day}-${params.tag}.json'}
"""


@pytest.fixture
def pipe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "in.csv").write_text("id\n1\n2\n")
    (tmp_path / "out").mkdir()
    text = PIPE.replace("  day: date\n", "  day: date\n  src: {type: str, default: in.csv}\n")
    (tmp_path / "p.yaml").write_text(text)
    return tmp_path


def test_params_substituted_and_typed():
    spec = agentl.parse(
        {
            "name": "x",
            "params": {"n": "int", "on": {"type": "bool", "default": False}},
            "chunk_size": "${params.n}",
            "steps": [{"id": "a", "use": "csv.read", "with": {"path": "f${params.n}-${params.on}.csv"}}],
        },
        params={"n": "3"},
    )
    assert spec.chunk_size == 3
    assert spec.steps[0].with_["path"] == "f3-false.csv"
    assert spec.params["n"].type == "int"


@pytest.mark.parametrize(
    "given,msg",
    [
        ({}, "missing required param"),
        ({"day": "2026-13-01"}, "expected date"),
        ({"day": "2026-01-01", "limit": "two"}, "expected int"),
        ({"day": "2026-01-01", "nope": "1"}, "unknown param"),
    ],
)
def test_params_validated_before_run(pipe, given, msg):
    with pytest.raises(SpecError, match=msg):
        agentl.run(pipe / "p.yaml", params=given, db=pipe / "s.db")
    assert not (pipe / "s.db").exists() or not list((pipe / "out").iterdir())


def test_run_api_and_cli(pipe):
    result = agentl.run(pipe / "p.yaml", params={"day": "2026-10-15"}, db=pipe / "s.db")
    summary = result.summary()
    assert summary["status"] == "succeeded" and summary["records"]["raw"] == 2
    assert (pipe / "out" / "2026-10-15-none.json").exists()
    code = main(["run", "p.yaml", "--db", "s.db", "--param", "day=2026-10-16", "--param", "tag=a=b"])
    assert code == 0 and (pipe / "out" / "2026-10-16-a=b.json").exists()
    assert main(["run", "p.yaml", "--db", "s.db", "--param", "limit=x", "--param", "day=2026-10-16"]) == 2
    assert main(["run", "p.yaml", "--db", "s.db", "--param", "oops"]) == 2


def test_lint_checks_params():
    bad = PIPE.replace("default: 2", "default: two").replace("${params.tag}", "${params.tga}")
    codes = [(f.code, f.message) for f in check_text(bad).errors]
    assert any(c == "AE010" and "expected int" in m for c, m in codes)
    assert any(c == "AE010" and "params.tga" in m and "tag" in m for c, m in codes)
    good = PIPE.replace("${params.src}", "in.csv")
    assert not check_text(good).errors


def test_periods():
    assert periods("2026-01-30", "2026-02-02") == ["2026-01-30", "2026-01-31", "2026-02-01", "2026-02-02"]
    assert periods("2026-11-15", "2027-02-01", "month") == [
        "2026-11-15",
        "2026-12-01",
        "2027-01-01",
        "2027-02-01",
    ]
    assert periods("2026-01-01", "2026-01-15", "week") == ["2026-01-01", "2026-01-08", "2026-01-15"]
    with pytest.raises(ValueError):
        periods("2026-02-01", "2026-01-01")


def test_backfill_dates_resumes(pipe, monkeypatch):
    days = periods("2026-01-01", "2026-01-04")
    import agentl.runner as runner

    real = runner.run
    calls = []
    crash = [True]

    def flaky(pipeline, **kw):
        calls.append(kw["params"]["day"])
        if kw["params"]["day"] == "2026-01-03" and crash.pop() if crash else False:
            raise KeyboardInterrupt  # the process dies mid-backfill
        return real(pipeline, **kw)

    monkeypatch.setattr(runner, "run", flaky)
    with pytest.raises(KeyboardInterrupt):
        backfill(pipe / "p.yaml", days, db=pipe / "s.db", kind="date")
    done_first = set(calls) - {"2026-01-03"}
    assert done_first == {"2026-01-01", "2026-01-02"}
    calls.clear()
    report = backfill(pipe / "p.yaml", days, db=pipe / "s.db", kind="date")
    assert report.ok and report.param == "day"
    assert set(report.skipped) == done_first and "2026-01-03" in report.succeeded
    assert not set(calls) & done_first  # finished days are not run again
    assert sorted(p.name for p in (pipe / "out").iterdir()) == [f"{d}-none.json" for d in days]
    again = backfill(pipe / "p.yaml", days, db=pipe / "s.db", kind="date")
    assert len(again.skipped) == 4 and not again.succeeded


def test_backfill_glob_cli(pipe, capsys):
    (pipe / "data").mkdir()
    for n in ("a", "b", "c"):
        (pipe / "data" / f"{n}.csv").write_text("id\n1\n")
    args = ["backfill", "p.yaml", "--db", "s.db", "--glob", "data/*.csv", "--param-name", "src"]
    args += ["--param", "day=2026-02-01", "--concurrency", "2", "--json"]
    assert main(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["succeeded"] == 3 and out["skipped"] == 0
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["skipped"] == 3
    bad = ["backfill", "p.yaml", "--db", "s.db", "--glob", "missing/*.csv", "--param", "day=2026-02-01"]
    assert main(bad) == 0  # nothing matches


def test_backfill_needs_param_name(pipe):
    with pytest.raises(SpecError, match="--param-name"):
        backfill(pipe / "p.yaml", ["in.csv"], params={"day": "2026-01-01"}, db=pipe / "s.db")


def test_backfill_reports_failures(pipe):
    report = backfill(
        pipe / "p.yaml",
        ["in.csv", "nope.csv"],
        param="src",
        params={"day": "2026-03-01"},
        db=pipe / "s.db",
    )
    assert report.succeeded == ["in.csv"] and "nope.csv" in report.failed
    assert "1 failed" in report.text()


# ------------------------------------------------------------- orchestrator examples, with fakes


class FakeCelery:
    def __init__(self):
        self.tasks = {}

    def task(self, name, **options):
        def wrap(fn):
            fn.options = options
            self.tasks[name] = fn
            return fn

        return wrap


def test_celery_example_with_fake_app(pipe):
    from agentl.integrations.celery import register

    app = FakeCelery()
    task = register(app)
    assert app.tasks["agentl.run"] is task and task.options["max_retries"] == 3
    out = task(str(pipe / "p.yaml"), db=str(pipe / "s.db"), params={"day": "2026-04-01"})
    assert out["status"] == "succeeded"
    json.dumps(out)
    with pytest.raises(SpecError):  # bad params are not a PipelineFailed, so not retried
        task(str(pipe / "p.yaml"), db=str(pipe / "s.db"), params={"day": "soon"})


def test_airflow_example_with_fake_operator(pipe, monkeypatch):
    class BaseOperator:
        def __init__(self, task_id, **kwargs):
            self.task_id = task_id

    fake = types.ModuleType("airflow.models")
    fake.BaseOperator = BaseOperator
    monkeypatch.setitem(sys.modules, "airflow", types.ModuleType("airflow"))
    monkeypatch.setitem(sys.modules, "airflow.models", fake)
    monkeypatch.delitem(sys.modules, "agentl.integrations.airflow", raising=False)
    mod = importlib.import_module("agentl.integrations.airflow")
    try:
        op = mod.AgentlOperator(
            task_id="daily",
            pipeline=str(pipe / "p.yaml"),
            run_params={"day": "2026-05-01"},
            db=str(pipe / "s.db"),
        )
        assert "run_params" in op.template_fields
        assert op.execute({})["status"] == "succeeded"
        assert (pipe / "out" / "2026-05-01-none.json").exists()
    finally:
        sys.modules.pop("agentl.integrations.airflow", None)
