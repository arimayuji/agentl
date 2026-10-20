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
    monkeypatch.setattr(report.subprocess, "run", refuse)  # never the real gh


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


TRACE = """Traceback (most recent call last):
  File "/venv/lib/agentl/runner.py", line 120, in run_step
    out = fn(rows)
  File "/venv/lib/agentl/connectors/files.py", line 88, in read_csv
    return rows[0].keys()
AttributeError: 'NoneType' object has no attribute 'keys'
"""


def test_fingerprint_uses_class_top_agentl_frame_and_version():
    fp = report.fingerprint(None, TRACE, "1.0.0")
    assert fp.startswith("agentl-fp-") and len(fp) == len("agentl-fp-") + 12
    moved = TRACE.replace("line 88", "line 90")  # line numbers do not matter
    assert report.fingerprint(None, moved, "1.0.0") == fp
    assert report.fingerprint(None, TRACE, "1.0.1") != fp
    assert report.fingerprint("TypeError: x", TRACE, "1.0.0") != fp
    assert report._top_frame(TRACE) == "agentl/connectors/files.py:read_csv"


def test_fingerprint_is_in_the_body(tmp_path, monkeypatch):
    path, run_id = _setup(tmp_path, monkeypatch)
    with Store(tmp_path / "s.db") as store:
        r = report.build(store, run_id, str(path))
    assert f"Fingerprint: `{r['fingerprint']}`" in r["markdown"]
    assert r["fingerprint"] in parse_qs(urlparse(r["url"]).query)["what"][0]


class FakeGh:
    def __init__(self, search="[]", create_code=0):
        self.calls = []
        self.search, self.create_code = search, create_code

    def __call__(self, cmd, input=None, **kw):
        import subprocess

        self.calls.append((cmd, input))
        if cmd[1] == "search":
            return subprocess.CompletedProcess(cmd, 0, self.search, "")
        return subprocess.CompletedProcess(
            cmd, self.create_code, "https://github.com/arimayuji/agentl/issues/999\n", "boom"
        )


REPORT = {"title": "[bug] x", "markdown": "body fp", "fingerprint": "agentl-fp-abc", "url": "https://u"}


def test_submit_without_gh_falls_back_to_url(monkeypatch):
    monkeypatch.setattr(report.shutil, "which", lambda name: None)
    got = report.submit(REPORT, lambda q: True, FakeGh())
    assert got == {"status": "no_gh", "url": "https://u"}
    assert "https://u" in report.submit_text(got)


def test_submit_shows_duplicates_and_files_nothing(monkeypatch):
    monkeypatch.setattr(report.shutil, "which", lambda name: "/usr/bin/gh")
    gh = FakeGh(search=json.dumps([{"number": 7, "title": "old", "url": "https://i/7", "state": "OPEN"}]))
    asked = []
    got = report.submit(REPORT, lambda q: asked.append(q) or True, gh)
    assert got["status"] == "duplicate" and not asked
    assert [c[0][1] for c in gh.calls] == ["search"] and '"agentl-fp-abc"' in gh.calls[0][0]
    assert "#7" in report.submit_text(got)


def test_submit_asks_then_creates(monkeypatch):
    monkeypatch.setattr(report.shutil, "which", lambda name: "/usr/bin/gh")
    gh = FakeGh()
    assert report.submit(REPORT, lambda q: False, gh)["status"] == "declined"
    assert [c[0][1] for c in gh.calls] == ["search"]
    got = report.submit(REPORT, lambda q: True, gh)
    assert got == {"status": "created", "url": "https://github.com/arimayuji/agentl/issues/999"}
    cmd, body = gh.calls[-1]
    assert cmd[1:3] == ["issue", "create"] and "--body-file" in cmd and body == "body fp"
    assert report.submit(REPORT, lambda q: True, FakeGh(create_code=1))["status"] == "failed"


def test_cli_submit_confirms(tmp_path, monkeypatch, capsys):
    _, run_id = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(report.shutil, "which", lambda name: "/usr/bin/gh")
    gh = FakeGh()
    monkeypatch.setattr(report.subprocess, "run", gh)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert main(["report", run_id, "--db", str(tmp_path / "s.db"), "--submit"]) == 0
    assert "Not filed" in capsys.readouterr().out and len(gh.calls) == 1
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert main(["report", run_id, "--db", str(tmp_path / "s.db"), "--submit"]) == 0
    assert "Filed: https://github.com/arimayuji/agentl/issues/999" in capsys.readouterr().out


def _broken_run(tmp_path, monkeypatch, extra=""):
    from agentl import connector

    @connector("test.report_broken2")
    def broken(input, **params):
        raise AttributeError("'NoneType' object has no attribute 'rows'")

    monkeypatch.chdir(tmp_path)
    (tmp_path / "p.yaml").write_text(f"name: x\n{extra}steps:\n  - id: a\n    use: test.report_broken2\n")
    return main(["run", "p.yaml", "--no-check", "--db", str(tmp_path / "s.db")])


def test_on_bug_off_prints_nothing(tmp_path, monkeypatch, capsys):
    assert _broken_run(tmp_path, monkeypatch, "report: {on_bug: off}\n") == 1
    assert "Looks like" not in capsys.readouterr().err


def test_on_bug_open_opens_the_form(tmp_path, monkeypatch, capsys):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    assert _broken_run(tmp_path, monkeypatch, "report: {on_bug: open}\n") == 1
    err = capsys.readouterr().err
    assert "Looks like an agentl bug" in err and "nothing is submitted" in err
    assert opened and "issues/new" in opened[0]


def test_on_bug_is_validated():
    from agentl.spec import SpecError, parse

    assert parse({"name": "x", "steps": []}).report.on_bug == "prompt"
    with pytest.raises((SpecError, ValueError)):
        parse({"name": "x", "steps": [], "report": {"on_bug": "always"}})
