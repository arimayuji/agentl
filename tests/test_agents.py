"""agentl for AI agents (#128): MCP tools, preview, the Claude Code skill."""

import json
import re
from pathlib import Path

import pytest

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


def test_mcp_registry_metadata_matches_the_package():
    """server.json is what the official MCP registry lists; it must name the released version and
    the README (the PyPI description) must carry the matching `mcp-name` line."""
    import agentl

    tomllib = pytest.importorskip("tomllib")  # Python 3.11+

    root = Path(agentl.__file__).parent.parent
    server = json.loads((root / "server.json").read_text())
    assert server["version"] == agentl.__version__
    pkg = server["packages"][0]
    assert (pkg["registryType"], pkg["identifier"], pkg["version"]) == (
        "pypi",
        "agentl-etl",
        agentl.__version__,
    )
    assert pkg["packageArguments"] == [{"type": "positional", "value": "mcp"}]
    assert len(server["description"]) <= 100
    assert f"<!-- mcp-name: {server['name']} -->" in (root / "README.md").read_text()
    scripts = tomllib.loads((root / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["agentl-etl"] == scripts["agentl"] == "agentl.cli:main"  # `uvx agentl-etl mcp`


def call_args(server, tool, args):
    """Like call, for tools that take an argument called `name`."""
    res = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}
    )
    return res["result"]["isError"], res["result"]["content"][0]["text"]


def test_mcp_templates_connectors_and_output(tmp_path, monkeypatch):
    """#164: start from a template without an LLM, read one connector, see what a run produced."""
    monkeypatch.chdir(tmp_path)
    s = setup(tmp_path)
    tools = {
        t["name"]: t for t in s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    }
    assert not tools["new_from_template"]["annotations"]["readOnlyHint"]
    assert tools["describe_connector"]["annotations"]["readOnlyHint"]
    assert "describe_connector" in tools["list_connectors"]["description"]
    assert "new_from_template" in tools["new_pipeline"]["description"]

    err, text = call(s, "list_templates")
    listed = json.loads(text)
    assert not err and listed and all(t["name"] and t["does"] for t in listed)

    name = listed[0]["name"]
    err, text = call_args(s, "new_from_template", {"name": name, "path": "sub/pipe.yaml"})
    assert not err and (tmp_path / "sub" / "pipe.yaml").is_file()
    assert lint.check_file(str(tmp_path / "sub" / "pipe.yaml"), warnings=False).errors == []
    err, text = call_args(s, "new_from_template", {"name": name, "path": "sub/pipe.yaml"})
    assert err and "overwrite" in text
    err, text = call_args(s, "new_from_template", {"name": name, "path": str(tmp_path.parent / "x.yaml")})
    assert err and "only writes files under" in text
    err, text = call_args(s, "new_from_template", {"name": "nope", "path": "n.yaml"})
    assert err and "no template" in text

    err, text = call_args(s, "describe_connector", {"name": "csv.read"})
    assert not err and text.startswith("csv.read") and "path" in text
    err, text = call_args(s, "describe_connector", {"name": "csv.reed"})
    assert err and "csv.read" in text

    err, text = call(s, "show_output", step="some")
    assert err and "no runs yet" in text
    err, text = call(s, "run_pipeline", pipeline=str(tmp_path / "p.yaml"))
    assert not err and json.loads(text)["status"] == "succeeded"
    err, text = call(s, "show_output", step="some", limit=1)
    shown = json.loads(text)
    assert not err and shown["total"] == 2 and shown["records"] == [{"id": "1"}]
    err, text = call(s, "show_output", step="nope")
    assert err and "no output kept" in text


def test_samples_are_clipped_and_can_be_turned_off(tmp_path, monkeypatch):
    from agentl import parse
    from agentl.runner import Runner
    from agentl.store import Store

    (tmp_path / "in.csv").write_text("id,text\n1," + "x" * 500 + "\n")
    spec = parse(
        {"name": "c", "steps": [{"id": "r", "use": "csv.read", "with": {"path": str(tmp_path / "in.csv")}}]}
    )
    with Store(tmp_path / "s.db") as store:
        r = Runner(spec, store=store).run()
        text = store.sample(r.run_id, "r")["records"][0]["text"]
        assert len(text) == 201 and text.endswith("…")
        monkeypatch.setenv("AGENTL_SAMPLES", "0")
        r = Runner(spec, store=store, use_cache=False).run()
        assert store.sample(r.run_id, "r") is None


def test_samples_mask_secret_values(tmp_path, monkeypatch):
    from agentl import parse, secrets
    from agentl.runner import Runner
    from agentl.store import Store

    token = 'tok"en\\with-quotes-' + "z" * 300  # quotes and backslash: masked before JSON and clipping
    monkeypatch.setenv("API_TOKEN", token)
    monkeypatch.setattr(secrets, "_ENV_SECRETS", set())
    (tmp_path / "in.json").write_text(json.dumps([{"echo": "Bearer " + token, "nested": {"t": token}}]))
    spec = parse(
        {
            "name": "m",
            "steps": [
                {"id": "r", "use": "json.read", "with": {"path": str(tmp_path / "in.json")}},
                {
                    "id": "s",
                    "use": "select",
                    "input": "r",
                    "with": {"fields": ["echo", "nested"], "note": "${API_TOKEN}"},
                },
            ],
        }
    )
    with Store(tmp_path / "s.db") as store:
        Runner(spec, store=store).run()
    stored = b"".join(p.read_bytes() for p in tmp_path.glob("s.db*"))
    assert b"zzzzzzzzzz" not in stored and b"***" in stored
