import json

import pytest

import agentl
from agentl import PipelineFailed


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "orch_steps.py").write_text("def boom(input=None):\n    raise RuntimeError('boom')\n")
    (tmp_path / "in.csv").write_text("id\n1\n2\n")
    (tmp_path / "ok.yaml").write_text(
        "name: ok\nsteps:\n"
        "  - {id: raw, use: csv.read, with: {path: '${SRC}'}}\n"
        "  - {id: out, use: json.write, input: raw, with: {path: '${DEST:-out.json}'}}\n"
    )
    (tmp_path / "bad.yaml").write_text("name: bad\nsteps:\n  - {id: x, fn: 'orch_steps:boom'}\n")
    return tmp_path


def test_run_from_path_with_vars(pipeline):
    result = agentl.run(pipeline / "ok.yaml", vars={"SRC": "in.csv"}, db=pipeline / "s.db")
    assert result.status == "succeeded"
    summary = result.summary()
    assert summary["records"] == {"raw": 2, "out": 1}
    assert "outputs" not in summary
    json.dumps(summary)  # serializable for task results
    assert json.loads((pipeline / "out.json").read_text())[1]["id"] == "2"


def test_raise_on_failure(pipeline):
    result = agentl.run(pipeline / "bad.yaml", db=pipeline / "s.db")
    assert result.status == "failed" and "boom" in result.error
    with pytest.raises(PipelineFailed) as e:
        agentl.run(pipeline / "bad.yaml", db=pipeline / "s.db", raise_on_failure=True)
    assert e.value.result is not None and e.value.result.status == "failed"


def test_celery_task_eager(pipeline):
    celery = pytest.importorskip("celery")
    from agentl.integrations.celery import register

    app = celery.Celery("t")
    app.conf.task_always_eager = True
    task = register(app, max_retries=0)
    out = task.delay(str(pipeline / "ok.yaml"), vars={"SRC": "in.csv"}, db=str(pipeline / "s.db")).get()
    assert out["status"] == "succeeded"
    with pytest.raises(PipelineFailed, match="boom"):
        task.delay(str(pipeline / "bad.yaml"), db=str(pipeline / "s.db")).get()


def test_temporal_activity(pipeline):
    testing = pytest.importorskip("temporalio.testing")
    from agentl.integrations.temporal import RunInput, run_pipeline

    env = testing.ActivityEnvironment()
    out = env.run(
        run_pipeline, RunInput(str(pipeline / "ok.yaml"), {"SRC": "in.csv"}, str(pipeline / "s.db"))
    )
    assert out["status"] == "succeeded"


def test_airflow_operator(pipeline):
    pytest.importorskip("airflow")
    from agentl.integrations.airflow import AgentlOperator

    op = AgentlOperator(task_id="t", pipeline=str(pipeline / "ok.yaml"), vars={"SRC": "in.csv"})
    assert op.execute({})["status"] == "succeeded"
