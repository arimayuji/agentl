"""`agentl mcp install|uninstall|status`: add the agentl MCP server to an MCP client's config file
without editing JSON by hand (#162).

Every other entry in the file is kept, a backup (`<file>.bak`) is written before a change, and
running the same install twice changes nothing. A file that is not plain JSON is never rewritten."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

NAME = "agentl"
CLIENTS = ("claude-desktop", "claude-code", "cursor", "vscode", "windsurf")


class InstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    client: str
    path: Path
    key: str  # "mcpServers", or "servers" for VS Code
    vscode: bool = False  # VS Code entries carry "type": "stdio"


def _platform() -> str:
    return "mac" if sys.platform == "darwin" else "win" if sys.platform.startswith("win") else "linux"


def _app_dir(home: Path, name: str, plat: str) -> Path:
    """Per-user application data folder: ~/Library/Application Support, %APPDATA% or ~/.config."""
    if plat == "mac":
        return home / "Library" / "Application Support" / name
    if plat == "win":
        return Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / name
    return Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / name


def target(
    client: str,
    project: bool = False,
    home: Path | None = None,
    cwd: Path | None = None,
    plat: str | None = None,
) -> Target:
    """Where `client` keeps its MCP servers: per user, or for this folder with `project`."""
    home = home or Path.home()
    cwd = cwd or Path.cwd()
    plat = plat or _platform()
    if client == "claude-desktop":
        if project:
            raise InstallError("Claude Desktop has no per-project config; drop --project")
        return Target(client, _app_dir(home, "Claude", plat) / "claude_desktop_config.json", "mcpServers")
    if client == "claude-code":
        return Target(client, cwd / ".mcp.json" if project else home / ".claude.json", "mcpServers")
    if client == "cursor":
        return Target(client, (cwd if project else home) / ".cursor" / "mcp.json", "mcpServers")
    if client == "vscode":
        path = cwd / ".vscode" / "mcp.json" if project else _app_dir(home, "Code", plat) / "User" / "mcp.json"
        return Target(client, path, "servers", vscode=True)
    if client == "windsurf":
        if project:
            raise InstallError("Windsurf has no per-project MCP config; drop --project")
        return Target(client, home / ".codeium" / "windsurf" / "mcp_config.json", "mcpServers")
    raise InstallError(f"unknown client {client!r}; one of: {', '.join(CLIENTS)}")


def command(
    root: str | None = None, allow_code: bool = False, db: str | None = None, uvx: bool = False
) -> list[str]:
    """The command the client starts: this Python's agentl (same version and extras as here), or
    `uvx agentl-etl` with --uvx."""
    if uvx:
        if not shutil.which("uvx"):
            raise InstallError("--uvx needs uv: https://docs.astral.sh/uv/")
        argv = ["uvx", "agentl-etl", "mcp"]
    else:
        argv = [sys.executable, "-m", "agentl", "mcp"]
    if root:
        argv += ["--root", str(Path(root).resolve())]
    if allow_code:
        argv.append("--allow-code")
    if db:
        if "://" in db and urlsplit(db).password:
            raise InstallError(
                "--db has a password, which would be written in plain text to the client's config. "
                "Set AGENTL_DB in the environment the client starts from instead."
            )
        argv += ["--db", db if "://" in db else str(Path(db).resolve())]
    return argv


def entry(t: Target, argv: list[str]) -> dict:
    e: dict[str, Any] = {"command": argv[0], "args": argv[1:]}
    return {"type": "stdio", **e} if t.vscode else e


def _read(path: Path) -> dict:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise InstallError(
            f"{path} is not plain JSON ({e.msg}, line {e.lineno}); not touching it. "
            "Remove the comments or trailing commas, or add the entry by hand."
        ) from None
    if not isinstance(data, dict):
        raise InstallError(f"{path}: expected a JSON object at the top level")
    return data


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # never a half-written config


def install(t: Target, argv: list[str]) -> str:
    """Add or update the agentl entry. Returns "added", "updated" or "unchanged"."""
    data = _read(t.path)
    servers = data.setdefault(t.key, {})
    if not isinstance(servers, dict):
        raise InstallError(f"{t.path}: {t.key!r} is not an object")
    new = entry(t, argv)
    if servers.get(NAME) == new:
        return "unchanged"
    state = "updated" if NAME in servers else "added"
    servers[NAME] = new
    _write(t.path, data)
    return state


def uninstall(t: Target) -> bool:
    data = _read(t.path)
    servers = data.get(t.key)
    if not isinstance(servers, dict) or NAME not in servers:
        return False
    del servers[NAME]
    _write(t.path, data)
    return True


def status(home: Path | None = None, cwd: Path | None = None, plat: str | None = None) -> list[dict]:
    """Every user and project config that has an agentl entry, with the command it runs."""
    out = []
    for client in CLIENTS:
        for project in (False, True):
            try:
                t = target(client, project, home, cwd, plat)
                servers = _read(t.path).get(t.key) or {}
            except InstallError:
                continue
            if isinstance(servers, dict) and NAME in servers:
                e = servers[NAME]
                out.append(
                    {
                        "client": client,
                        "scope": "project" if project else "user",
                        "path": str(t.path),
                        "command": " ".join([e.get("command", ""), *e.get("args", [])]),
                    }
                )
    return out


RESTART = {
    "claude-desktop": "Quit Claude Desktop fully and open it again.",
    "claude-code": "Start a new Claude Code session (or run /mcp).",
    "cursor": "Open Cursor Settings > MCP and refresh, or restart Cursor.",
    "vscode": "Run 'MCP: List Servers' in VS Code, or reload the window.",
    "windsurf": "Refresh the MCP servers in Windsurf's Cascade panel, or restart Windsurf.",
}


def claude_cli(argv: list[str], project: bool) -> bool:
    """Use `claude mcp add-json` when the Claude Code CLI is installed (it owns ~/.claude.json).
    Returns False when it is not, so the caller edits the file."""
    claude = shutil.which("claude")
    if not claude:
        return False
    scope = "project" if project else "user"
    subprocess.run([claude, "mcp", "remove", NAME, "-s", scope], capture_output=True)
    run = subprocess.run(
        [
            claude,
            "mcp",
            "add-json",
            NAME,
            json.dumps({"type": "stdio", "command": argv[0], "args": argv[1:]}),
            "-s",
            scope,
        ],
        capture_output=True,
        text=True,
    )
    if run.returncode:
        raise InstallError(f"claude mcp add-json failed: {(run.stderr or run.stdout).strip()}")
    return True
