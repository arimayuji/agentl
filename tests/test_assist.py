"""`agentl new`, `agentl doctor` and the `agentl mcp` server (#19)."""

import json
import sys

import pytest

from agentl import assist, parse
from agentl.cli import main
from agentl.mcp import MCPClient
from agentl.runner import Runner
from agentl.spec import MCPServerSpec, SpecError
from agentl.store import Store

GOOD = """name: orders
steps:
  - id: read
    use: csv.read
    with: {path: orders.csv}
  - id: save
    use: json.write
    input: read
    with: {path: out.json}
"""


class Scripted:
    """Answers `text()` from a list and records what it was asked."""

    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def text(self, instruction, content):
        self.prompts.append((instruction, content))
        return self.answers.pop(0)


# ---------------------------------------------------------------- new
def test_new_retries_with_the_validation_error():
    bad = GOOD.replace("csv.read", "csv.reed")
    llm = Scripted(f"```yaml\n{bad}```", f"Here you go:\n```yaml\n{GOOD}```")
    text, spec = assist.new_pipeline("read orders.csv and write json", llm)
    assert text == GOOD and [s.id for s in spec.steps] == ["read", "save"]
    instruction, retry = llm.prompts[1]
    assert "- csv.read(" in instruction and "unknown connector 'csv.reed'" in retry


def test_new_gives_up_after_the_attempts():
    llm = Scripted("not: [a pipeline", "name: x", "steps: []")
    with pytest.raises(SpecError, match="no valid pipeline after 3 attempts"):
        assist.new_pipeline("anything", llm)


def test_new_cli_writes_the_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(assist, "llm_from_env", lambda *a: Scripted(GOOD))
    out = tmp_path / "p.yaml"
    assert main(["new", "orders to json", "-o", str(out)]) == 0
    assert out.read_text() == GOOD and "flowchart" in capsys.readouterr().err
    assert main(["new", "again", "-o", str(out)]) == 2  # never overwrites


# ---------------------------------------------------------------- doctor
def broken(input=None):
    raise RuntimeError("connection refused by crm.example.com")


def _failed_run(tmp_path):
    spec = parse(
        {
            "name": "crm-sync",
            "steps": [
                {"id": "rows", "use": "csv.read", "with": {"path": str(tmp_path / "in.csv")}},
                {"id": "push", "fn": "tests.test_assist:broken", "input": "rows"},
            ],
        }
    )
    (tmp_path / "in.csv").write_text("a\n1\n")
    with Store(tmp_path / "s.db") as store:
        return Runner(spec, store=store).run().run_id


def test_doctor_explains_from_the_store(tmp_path):
    run_id = _failed_run(tmp_path)
    with Store(tmp_path / "s.db") as store:
        report = assist.diagnose(store, run_id)
        assert [r["id"] for r in assist.failures(store)] == [run_id]
    assert report["failed_step"] == "push" and report["steps_done"] == ["rows"]
    text = assist.explain(report)
    assert "failed at step 'push'" in text and "connection refused" in text
    llm = Scripted("Check that crm.example.com is reachable.")
    assert "suggestion:\nCheck" in assist.explain(report, llm, "name: crm-sync")
    assert "connection refused" in llm.prompts[0][1] and "name: crm-sync" in llm.prompts[0][1]


def test_doctor_cli_defaults_to_the_latest_failure(tmp_path, capsys):
    _failed_run(tmp_path)
    assert main(["doctor", "--db", str(tmp_path / "s.db")]) == 0
    assert "failed at step 'push'" in capsys.readouterr().out


# ---------------------------------------------------------------- mcp server
def noisy(input=None):
    print("this must not reach the MCP stdout")
    return [{"ok": True}]


def test_mcp_server_round_trip_with_our_own_client(tmp_path):
    pipeline = tmp_path / "p.yaml"
    pipeline.write_text("name: hello\nsteps:\n  - id: say\n    fn: tests.test_assist:noisy\n")
    db = str(tmp_path / "runs.db")
    client = MCPClient(
        "agentl",
        MCPServerSpec(command=[sys.executable, "-m", "agentl.cli", "mcp", "--db", db], timeout=60),
    )
    try:
        tools = {t["name"]: t for t in client.list_tools()}
        assert {"validate", "run_pipeline", "get_run", "list_failures", "doctor"} <= set(tools)
        assert tools["validate"]["annotations"]["readOnlyHint"] is True
        assert tools["run_pipeline"]["annotations"]["readOnlyHint"] is False
        assert client.call("validate", {"pipeline": str(pipeline)}) == "ok: hello (1 steps)"
        assert client.call("validate", {"pipeline": GOOD.replace("csv.read", "nope")}).startswith(
            "error: unknown connector 'nope'"
        )
        run = json.loads(client.call("run_pipeline", {"pipeline": str(pipeline)}))
        assert run["status"] == "succeeded" and run["records"] == {"say": 1}
        events = json.loads(client.call("get_run", {"run_id": run["run_id"]}))["events"]
        assert events[0]["type"] == "run.started"
        assert json.loads(client.call("list_failures", {})) == []
        assert client.call("get_run", {"run_id": "nope"}).startswith("error:")
    finally:
        client.close()


def test_mcp_server_protocol_errors():
    from agentl.mcp_server import Server

    s = Server(":memory:")
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert s.handle({"jsonrpc": "2.0", "id": 1, "method": "nope"})["error"]["code"] == -32601
    res = s.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "x"}})
    assert res["error"]["code"] == -32602
