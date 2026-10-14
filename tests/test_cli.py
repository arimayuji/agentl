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
