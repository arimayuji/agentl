"""agentl mcp install / uninstall / status (#162): config files per client and OS, other entries
kept, a backup before each change, idempotent, never rewriting a file it cannot parse."""

import json
import sys

import pytest

from agentl import mcp_install as mi
from agentl.cli import main

ARGV = [sys.executable, "-m", "agentl", "mcp"]


@pytest.mark.parametrize(
    ("client", "plat", "project", "expected"),
    [
        (
            "claude-desktop",
            "mac",
            False,
            "home/Library/Application Support/Claude/claude_desktop_config.json",
        ),
        ("claude-desktop", "linux", False, "home/.config/Claude/claude_desktop_config.json"),
        ("claude-desktop", "win", False, "appdata/Claude/claude_desktop_config.json"),
        ("claude-code", "linux", False, "home/.claude.json"),
        ("claude-code", "linux", True, "proj/.mcp.json"),
        ("cursor", "mac", False, "home/.cursor/mcp.json"),
        ("cursor", "win", True, "proj/.cursor/mcp.json"),
        ("vscode", "mac", False, "home/Library/Application Support/Code/User/mcp.json"),
        ("vscode", "win", False, "appdata/Code/User/mcp.json"),
        ("vscode", "linux", True, "proj/.vscode/mcp.json"),
        ("windsurf", "linux", False, "home/.codeium/windsurf/mcp_config.json"),
    ],
)
def test_config_paths(tmp_path, monkeypatch, client, plat, project, expected):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    t = mi.target(client, project, tmp_path / "home", tmp_path / "proj", plat)
    assert t.path == tmp_path / expected
    assert t.key == ("servers" if client == "vscode" else "mcpServers")


def test_no_project_scope_where_the_client_has_none(tmp_path):
    for client in ("claude-desktop", "windsurf"):
        with pytest.raises(mi.InstallError, match="--project"):
            mi.target(client, True, tmp_path, tmp_path, "linux")


def test_install_keeps_other_entries_backs_up_and_is_idempotent(tmp_path):
    t = mi.target("cursor", False, tmp_path, tmp_path, "linux")
    t.path.parent.mkdir(parents=True)
    original = {"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}
    t.path.write_text(json.dumps(original))
    assert mi.install(t, ARGV) == "added"
    data = json.loads(t.path.read_text())
    assert data["theme"] == "dark" and data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["agentl"] == {"command": ARGV[0], "args": ARGV[1:]}
    assert json.loads(t.path.with_name("mcp.json.bak").read_text()) == original
    before = t.path.read_text()
    assert mi.install(t, ARGV) == "unchanged" and t.path.read_text() == before
    assert mi.install(t, [*ARGV, "--allow-code"]) == "updated"
    assert mi.uninstall(t) and "agentl" not in json.loads(t.path.read_text())["mcpServers"]
    assert not mi.uninstall(t)


def test_vscode_entries_are_typed(tmp_path):
    t = mi.target("vscode", True, tmp_path, tmp_path, "linux")
    mi.install(t, ARGV)
    assert json.loads(t.path.read_text())["servers"]["agentl"]["type"] == "stdio"


def test_a_file_that_is_not_plain_json_is_left_alone(tmp_path):
    t = mi.target("vscode", True, tmp_path, tmp_path, "linux")
    t.path.parent.mkdir(parents=True)
    t.path.write_text('{\n  // my servers\n  "servers": {}\n}\n')
    with pytest.raises(mi.InstallError, match="not plain JSON"):
        mi.install(t, ARGV)
    assert "// my servers" in t.path.read_text()


def test_command_options(tmp_path):
    argv = mi.command(root=str(tmp_path), allow_code=True, db="runs.db")
    assert argv[:4] == ARGV and "--allow-code" in argv
    assert argv[argv.index("--root") + 1] == str(tmp_path.resolve())
    assert argv[argv.index("--db") + 1].endswith("runs.db")
    assert mi.command(db="postgresql://h/db")[-1] == "postgresql://h/db"
    with pytest.raises(mi.InstallError, match="plain text"):
        mi.command(db="postgresql://u:secret@h/db")


def test_status_lists_installed_clients(tmp_path):
    home, proj = tmp_path / "home", tmp_path / "proj"
    mi.install(mi.target("cursor", True, home, proj, "linux"), ARGV)
    rows = mi.status(home, proj, "linux")
    assert [(r["client"], r["scope"]) for r in rows] == [("cursor", "project")]
    assert rows[0]["command"] == " ".join(ARGV)


def test_cli_round_trip(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mi.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert main(["mcp", "install", "--client", "cursor", "--project"]) == 0
    out = capsys.readouterr().out
    assert "added: agentl in" in out and "Cursor" in out
    assert main(["mcp", "install", "--client", "cursor", "--project"]) == 0
    assert "unchanged" in capsys.readouterr().out
    assert main(["mcp", "status"]) == 0
    assert "cursor (project)" in capsys.readouterr().out
    assert main(["mcp", "uninstall", "--client", "cursor", "--project"]) == 0
    assert main(["mcp", "install"]) == 2  # --client is required


def test_python_dash_m_agentl_runs_the_cli():
    import subprocess

    out = subprocess.run([sys.executable, "-m", "agentl", "--version"], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.startswith("agentl ")
