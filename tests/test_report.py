"""`agentl report`: a local, ready-to-paste bug report; nothing goes over the network."""

import json
from urllib.parse import parse_qs, urlparse

import pytest

from agentl import report
from agentl.cli import main
from agentl.mcp_server import Server
from agentl.runner import Runner
from agentl.spec import load
from agentl.store import Store

SECRET = "sk-live-0123456789abcdef"

PIPELINE = """name: orders
steps:
  - id: rows
    use: csv.read
    with: {path: orders.csv}
  - id: total
    fn: tests.test_report:boom
    input: rows
    with:
      api_token: hardcoded-token-123
      dsn: ${ORDERS_DSN:-postgresql://user:pw@localhost/db}
"""


def boom(input, **params):
    raise TypeError("unsupported operand type(s) for +: 'int' and 'str'")


def _setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CRM_API_TOKEN", SECRET)
    (tmp_path / "orders.csv").write_text("id,amount\n1,10\n")
    path = tmp_path / "pipeline.yaml"
    path.write_text(PIPELINE)
    with Store(tmp_path / "s.db") as store:
        run_id = Runner(load(path), store=store).run().run_id
    return path, run_id


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("agentl report must not use the network")

    monkeypatch.setattr("socket.socket.connect", refuse)


def test_mask_yaml():
    out = report.mask_yaml(PIPELINE)
    assert "hardcoded-token-123" not in out and "api_token: ***" in out
    assert "dsn: ${ORDERS_DSN}" in out and "pw@localhost" not in out
    assert "use: csv.read" in out


def test_report_has_every_form_field(tmp_path, monkeypatch):
    path, run_id = _setup(tmp_path, monkeypatch)
    with Store(tmp_path / "s.db") as store:
        r = report.build(store, run_id, str(path))
    assert r["failed_step"] == "total" and r["step_kind"] == "fn"
    assert "TypeError" in r["error"] and "Traceback" in r["traceback"] and "boom" in r["traceback"]
    assert not r["likely_bug"]  # the error came from the user's fn: step
    md = r["markdown"]
    for label in ("What happened?", "Minimal pipeline", "Output or traceback", "agentl version"):
        assert label in md
    assert "hardcoded-token-123" not in md and SECRET not in json.dumps(r)
    q = parse_qs(urlparse(r["url"]).query)
    assert q["template"] == ["bug_report.yml"] and q["title"][0].startswith("[bug] TypeError")
    assert {"what", "repro", "logs", "version", "env"} <= set(q)


def test_cli_report_json_and_output(tmp_path, monkeypatch, capsys):
    path, run_id = _setup(tmp_path, monkeypatch)
    db = str(tmp_path / "s.db")
    assert main(["report", "--pipeline", str(path), "--db", db, "--json", "-o", "r.md"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["run_id"] == run_id and "Nothing was sent" in got["notice"]
    assert (tmp_path / "r.md").read_text() == got["markdown"]
    assert main(["report", run_id, "--db", db]) == 0
    out = capsys.readouterr().out
    assert "issues/new?template=bug_report.yml" in out and "CONTRIBUTING_AI.md" in out


def test_long_report_asks_to_paste(tmp_path, monkeypatch, capsys):
    _, run_id = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(report, "URL_LIMIT", 100)
    assert main(["report", run_id, "--db", str(tmp_path / "s.db"), "-o", "bug.md"]) == 0
    out = capsys.readouterr().out
    assert "too long for a URL" in out and "paste bug.md" in out


def test_likely_bug():
    assert report.likely_bug("AttributeError: 'NoneType' object has no attribute 'x'")
    assert not report.likely_bug("AttributeError: x", "fn")
    assert not report.likely_bug("FileNotFoundError: orders.csv")
    assert not report.likely_bug(None)


def test_crash_hint_and_report(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    def broken(*a, **k):
        raise AttributeError("internal oops")

    monkeypatch.setattr("agentl.cli._gaps", broken)
    (tmp_path / "p.yaml").write_text("name: x\nsteps: []\n")
    assert main(["gaps", "p.yaml"]) == 1
    err = capsys.readouterr().err
    assert "Looks like an agentl bug" in err and "agentl report --crash" in err
    assert main(["report", "--crash", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert "internal oops" in got["traceback"] and got["likely_bug"]

    monkeypatch.setenv(report.QUIET_ENV, "1")
    assert main(["gaps", "p.yaml"]) == 1
    assert "Looks like" not in capsys.readouterr().err


def test_user_errors_get_no_hint(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["run", "missing.yaml", "--no-check"]) == 2
    assert "Looks like" not in capsys.readouterr().err


def test_mcp_report_is_read_only(tmp_path, monkeypatch):
    path, run_id = _setup(tmp_path, monkeypatch)
    server = Server(db=str(tmp_path / "s.db"), root=str(tmp_path))
    meta, fn = server.tools["report"]
    assert meta["annotations"]["readOnlyHint"] is True
    got = fn({"pipeline": str(path)})
    assert got["run_id"] == run_id and path.read_text() == PIPELINE


def test_failed_run_with_internal_error_prints_hint(tmp_path, monkeypatch, capsys):
    from agentl import connector

    @connector("test.report_broken")
    def broken(input, **params):
        raise AttributeError("'NoneType' object has no attribute 'rows'")

    monkeypatch.chdir(tmp_path)
    (tmp_path / "p.yaml").write_text("name: x\nsteps:\n  - id: a\n    use: test.report_broken\n")
    assert main(["run", "p.yaml", "--no-check", "--db", str(tmp_path / "s.db")]) == 1
    err = capsys.readouterr().err
    assert "Looks like an agentl bug: run `agentl report " in err
