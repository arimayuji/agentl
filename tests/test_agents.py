"""agentl for AI agents (#128): MCP tools, preview, the Claude Code skill."""

import json
import re
from pathlib import Path

from agentl import assist, lint
from agentl.cli import main
from agentl.mcp_server import Server

PIPE = """name: p
steps:
  - id: rows
    use: csv.read
    with: { path: in.csv }
  - id: some
    use: select
    input: rows
    with: { fields: [id] }
  - id: save
    use: json.write
    input: some
    with: { path: out.json }
"""


def call(server, name, **args):
    res = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
    )
    text = res["result"]["content"][0]["text"]
    return res["result"]["isError"], text


def setup(tmp_path):
    (tmp_path / "in.csv").write_text("id,text\n1,a\n2,b\n")
    (tmp_path / "p.yaml").write_text(PIPE)
    return Server(str(tmp_path / "runs.db"), root=str(tmp_path))


def test_mcp_plan_preview_and_lint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = setup(tmp_path)
    tools = {
        t["name"]: t for t in s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    }
    assert {"lint", "plan", "preview_step", "new_pipeline"} <= set(tools)
    assert all(
        tools[t]["annotations"]["readOnlyHint"] for t in ("lint", "plan", "preview_step", "new_pipeline")
    )

    err, text = call(s, "plan", pipeline=str(tmp_path / "p.yaml"))
    steps = {x["step"]: x for x in json.loads(text)["steps"]}
    assert not err and steps["some"]["records"] == 2 and steps["save"]["status"] == "writes"
    assert not (tmp_path / "out.json").exists()

    err, text = call(s, "preview_step", pipeline=str(tmp_path / "p.yaml"), step="some", n=1)
    shown = json.loads(text)
    assert not err and shown["input"]["rows"]["first"] == [{"id": "1", "text": "a"}]
    assert shown["output"] == {"records": 2, "first": [{"id": "1"}]}
    err, text = call(s, "preview_step", pipeline=str(tmp_path / "p.yaml"), step="som")
    assert err and "Did you mean 'some'?" in text

    err, text = call(s, "lint", pipeline=PIPE.replace("csv.read", "csv.reed"))
    found = json.loads(text)
    assert not err and not found["ok"] and found["findings"][0]["fix"] == "use: csv.read"

    outside = Server(str(tmp_path / "runs.db"), root=str(tmp_path / "sub"))
    err, text = call(outside, "plan", pipeline=str(tmp_path / "p.yaml"))
    assert err and "only runs files under" in text


def test_mcp_new_pipeline_returns_yaml(tmp_path, monkeypatch):
    class Scripted:
        def text(self, instruction, content):
            return f"```yaml\n{PIPE}```"

    monkeypatch.setattr(assist, "llm_from_env", lambda *a: Scripted())
    err, text = call(setup(tmp_path), "new_pipeline", description="csv to json")
    assert not err and "csv.read" in text and "llm:" in text


def test_cli_preview_and_skill_install(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    setup(tmp_path)
    assert main(["preview", "p.yaml", "some", "-n", "1", "--db", str(tmp_path / "s.db")]) == 0
    assert json.loads(capsys.readouterr().out)["output"]["records"] == 2
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))  # what Path.home() reads on Windows
    assert main(["skill", "install"]) == 0
    installed = tmp_path / "home" / ".claude" / "skills" / "agentl" / "SKILL.md"
    assert installed.read_text().startswith("---\nname: agentl\n")
    assert main(["skill", "install", "--project"]) == 0
    assert (tmp_path / ".claude" / "skills" / "agentl" / "SKILL.md").exists()


def test_the_skill_example_is_a_valid_pipeline():
    import agentl

    text = (Path(agentl.__file__).parent / "skill" / "SKILL.md").read_text()
    example = re.search(r"```yaml\n(.*?)```", text, re.S).group(1)
    report = lint.check_text(example)
    assert not report.errors, report.render()
    used = set(re.findall(r"use: ([a-z]+\.[a-z]+)", text))
    assert used <= set(__import__("agentl.registry", fromlist=["names"]).names())
