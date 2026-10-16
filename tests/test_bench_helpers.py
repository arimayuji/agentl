"""Small features the agent benchmark called for (#143): SQLite upsert, `agentl connectors <name>`."""

import sqlite3

import pytest

from agentl import registry
from agentl.cli import main
from agentl.registry import get


def test_sqlite_upsert_replaces_rows_by_key(tmp_path):
    db = str(tmp_path / "e.db")
    write = get("sqlite.write")
    write([{"id": "E-1", "status": "open"}, {"id": "E-2", "status": "open"}], db=db, table="events")
    out = write(
        [{"id": "E-2", "status": "paid"}, {"id": "E-3", "status": "open"}, {"id": "E-3", "status": "late"}],
        db=db,
        table="events",
        mode="upsert",
        key="id",
    )
    rows = sqlite3.connect(db).execute("select id, status from events order by id").fetchall()
    assert rows == [("E-1", "open"), ("E-2", "paid"), ("E-3", "late")]  # last one wins within a batch
    assert out == [{"table": "events", "written": 2}]


def test_sqlite_upsert_needs_a_key_and_a_known_mode(tmp_path):
    with pytest.raises(ValueError, match="needs `key`"):
        get("sqlite.write")([{"id": 1}], db=str(tmp_path / "x.db"), table="t", mode="upsert")
    with pytest.raises(ValueError, match="append, replace or upsert"):
        get("sqlite.write")([{"id": 1}], db=str(tmp_path / "x.db"), table="t", mode="merge")


def test_connectors_name_shows_parameters(capsys):
    text = registry.describe("http.get")
    assert text.startswith("http.get") and "paginate: { param: page, next: next_page }" in text
    assert "  url (str)" in text and "  retries (int) = 2" in text
    assert "  key (str | list[str] | None)" in registry.describe("sqlite.write")
    assert main(["connectors", "filter"]) == 0
    assert "need a `fn:` step" in capsys.readouterr().out
    assert main(["connectors"]) == 0 and "csv.read" in capsys.readouterr().out


BROKEN = """name: t
steps:
  - id: read
    use: csv.reed
    with: { path: in.csv }
  - id: save
    use: json.write
    input: nowhere
    with: { path: out.json }
"""


def test_run_checks_first_and_shows_only_the_first_error(tmp_path, capsys, monkeypatch):
    """#147: a broken pipeline stops before running, with one error and its fix."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "p.yaml").write_text(BROKEN)
    assert main(["run", "p.yaml", "--db", str(tmp_path / "s.db")]) == 2
    err = capsys.readouterr().err
    assert err.count(" error AE") == 1 and "fix:" in err
    assert "1 more error(s): agentl lint p.yaml" in err and "the run did not start" in err
    assert not (tmp_path / "s.db").exists()  # nothing ran


def test_run_with_no_check_skips_the_lint(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "in.csv").write_text("id\n1\n")
    (tmp_path / "p.yaml").write_text(
        "name: t\nsteps:\n  - id: read\n    use: csv.read\n    with: { path: in.csv }\n"
        "  - id: save\n    use: json.write\n    input: read\n    with: { path: out.json }\n"
    )
    assert main(["run", "p.yaml", "--db", str(tmp_path / "s.db")]) == 0
    assert (tmp_path / "out.json").is_file()
    assert main(["run", "p.yaml", "--no-check", "--db", str(tmp_path / "s.db")]) == 0
