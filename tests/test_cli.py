import pathlib
import shutil

from agentl.cli import main

ROOT = pathlib.Path(__file__).parents[1]


def test_cli_commands(tmp_path, monkeypatch, capsys):
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    monkeypatch.chdir(tmp_path)
    db = ["--db", str(tmp_path / "db.sqlite")]

    assert main(["validate", "examples/quickstart.yaml", *db]) == 0
    assert "ok: invoices" in capsys.readouterr().out

    assert main(["diagram", "examples/quickstart.yaml"]) == 0
    assert "flowchart LR" in capsys.readouterr().out

    assert main(["run", "examples/quickstart.yaml", *db]) == 0
    assert "run.succeeded" in capsys.readouterr().out

    assert main(["runs", *db]) == 0
    assert "succeeded" in capsys.readouterr().out

    assert main(["connectors"]) == 0
    assert "csv.read" in capsys.readouterr().out


def test_cli_reports_bad_spec(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\nsteps:\n  - id: a\n")
    assert main(["validate", str(bad)]) == 2
    assert "error:" in capsys.readouterr().err


def test_version(capsys):
    import pytest

    from agentl import __version__

    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0 and capsys.readouterr().out.strip() == f"agentl {__version__}"


def test_writers_report_records_written(tmp_path):
    from agentl import parse
    from agentl.runner import Runner
    from agentl.store import Store

    (tmp_path / "in.csv").write_text("a\n1\n2\n3\n")
    spec = parse(
        {
            "name": "w",
            "steps": [
                {"id": "r", "use": "csv.read", "with": {"path": str(tmp_path / "in.csv")}},
                {"id": "s", "use": "json.write", "input": "r", "with": {"path": str(tmp_path / "o.json")}},
            ],
        }
    )
    with Store(tmp_path / "s.db") as store:
        run = Runner(spec, store=store).run()
        done = [e for e in store.events(run.run_id) if e["type"] == "step.succeeded" and e["step"] == "s"]
    assert done[0]["data"]["written"] == 3


def test_dashboard_counts_records_written():
    from agentl.dashboard import step_stats

    events = [{"step": "s", "type": "step.succeeded", "data": {"records": 1, "written": 3, "seconds": 0}}]
    assert step_stats(events)["s"]["records"] == 3
