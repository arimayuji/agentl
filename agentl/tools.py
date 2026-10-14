"""Tools agent steps can call: built-in ones, your Python functions and MCP servers.

    from agentl import tool

    @tool(read_only=True)
    def crm_company(domain: str) -> dict:
        "Company record from the CRM by web domain."
        return crm.get(domain)

Use it in YAML as `tools: [fn:my_package.tools:crm_company]`. Tools are read-only by default:
one that can write must be declared with `read_only=False` and listed in the step's
`write_tools`."""

from __future__ import annotations

import functools
import inspect
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, get_type_hints

from . import registry
from .spec import PipelineSpec, StepSpec

MAX_RESULT_CHARS = 20_000

_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    fn: Callable[[dict], Any]
    read_only: bool

    def run(self, args: dict) -> str:
        """The result as text for the model, cut to MAX_RESULT_CHARS."""
        out = self.fn(args)
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
        return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + "\n[truncated]"


def _schema(fn: Callable[..., Any]) -> dict:
    hints = get_type_hints(fn)
    props: dict[str, dict] = {}
    required = []
    for name, p in inspect.signature(fn).parameters.items():
        typ = hints.get(name, str)
        origin = getattr(typ, "__origin__", typ)
        props[name] = {"type": _JSON_TYPES.get(origin, "string")}
        if p.default is inspect.Parameter.empty:
            required.append(name)
    return {"type": "object", "properties": props, "required": required}


def tool(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    read_only: bool = True,
) -> Any:
    """Mark a function as an agent tool. Its signature becomes the input schema and its docstring
    the description the model sees."""

    def wrap(f: Callable[..., Any]) -> Callable[..., Any]:
        f.agentl_tool = Tool(  # type: ignore[attr-defined]
            name=name or f.__name__,
            description=(description or inspect.getdoc(f) or f.__name__).strip(),
            input_schema=_schema(f),
            fn=lambda args: f(**args),
            read_only=read_only,
        )
        return f

    return wrap(fn) if fn is not None else wrap


# ---------------------------------------------------------------- built-in tools
def _http_get(settings: dict) -> Tool:
    hosts = [h.lower() for h in settings.get("allow_hosts", [])]
    headers = settings.get("headers", {})
    max_bytes = int(settings.get("max_bytes", 200_000))

    def get(args: dict) -> str:
        import urllib.parse
        import urllib.request

        from . import netguard  # imported here: urllib.request stays out of `import agentl`

        url = str(args["url"])

        def allowed(target: str) -> None:  # the URL and every redirect it leads to
            host = (urllib.parse.urlparse(target).hostname or "").lower()
            if hosts and not any(host == h or host.endswith("." + h) for h in hosts):
                raise netguard.BlockedURL(f"{host} is not in this tool's allow_hosts")

        try:
            allowed(url)
        except netguard.BlockedURL as e:
            return f"error: {e}"
        req = urllib.request.Request(url, headers=headers)
        with netguard.urlopen(req, timeout=float(settings.get("timeout", 20)), check=allowed) as resp:
            body = resp.read(max_bytes).decode("utf-8", errors="replace")
            return f"HTTP {resp.status}\n{body}"

    return Tool(
        "http.get",
        "GET a URL and return the status and the start of the body. Public addresses only"
        + (f"; allowed hosts: {', '.join(hosts)}" if hosts else "")
        + ".",
        {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
        get,
        read_only=True,
    )


def _sqlite_query(settings: dict) -> Tool:
    db = settings.get("db")
    if not db:
        raise ValueError("tool sqlite.query needs a db: {sqlite.query: {db: path/to.db}}")
    limit = int(settings.get("max_rows", 200))

    def query(args: dict) -> Any:
        import sqlite3
        from pathlib import Path

        # mode=ro: the connection itself cannot write, whatever the SQL says
        uri = f"file:{Path(db).resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(str(args["sql"]), args.get("params") or []).fetchmany(limit)
            return [dict(r) for r in rows]

    return Tool(
        "sqlite.query",
        f"Run a read-only SQL query on the SQLite database {_basename(db)} and return up to {limit} rows.",
        {
            "type": "object",
            "properties": {"sql": {"type": "string"}, "params": {"type": "array", "items": {}}},
            "required": ["sql"],
        },
        query,
        read_only=True,
    )


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


BUILTINS: dict[str, Callable[[dict], Tool]] = {"http.get": _http_get, "sqlite.query": _sqlite_query}


# ---------------------------------------------------------------- resolution
_NAME_OK = re.compile(r"[^a-zA-Z0-9_-]")


def api_name(name: str) -> str:
    """Tool names providers accept: letters, digits, `_` and `-`, at most 64 chars."""
    return _NAME_OK.sub("_", name)[:64]


def _mcp_call(client: Any, name: str, args: dict) -> str:
    return client.call(name, args)


class ToolBox:
    """Builds the tools of each agent step and owns the MCP connections of a run."""

    def __init__(self, spec: PipelineSpec):
        self.spec = spec
        self._mcp: dict[str, Any] = {}

    def _client(self, server: str) -> Any:
        if server not in self._mcp:
            from .mcp import MCPClient

            self._mcp[server] = MCPClient(server, self.spec.mcp[server])
        return self._mcp[server]

    def for_step(self, step: StepSpec) -> list[Tool]:
        tools: list[Tool] = []
        for item in step.tools:
            name, settings = (item, {}) if isinstance(item, str) else next(iter(item.items()))
            if name in BUILTINS:
                tools.append(BUILTINS[name](settings or {}))
            elif name.startswith("fn:"):
                fn = registry.resolve_fn(name[3:], self.spec.base_dir)
                t = getattr(fn, "agentl_tool", None)
                if t is None:
                    t = tool(fn, read_only=False).agentl_tool  # undeclared functions may write
                tools.append(t)
            elif name.startswith("mcp:"):
                tools += self._mcp_tools(step, name[4:])
            else:
                raise ValueError(f"step '{step.id}': unknown tool '{name}'")
        blocked = [t.name for t in tools if not t.read_only and t.name not in step.write_tools]
        if blocked:
            raise ValueError(
                f"step '{step.id}': {blocked} can write; list them in write_tools to allow it, "
                "or mark read-only functions with @tool(read_only=True)"
            )
        seen = set()
        for t in tools:
            if api_name(t.name) in seen:
                raise ValueError(f"step '{step.id}': two tools are named '{api_name(t.name)}'")
            seen.add(api_name(t.name))
        return tools

    def _mcp_tools(self, step: StepSpec, ref: str) -> list[Tool]:
        server, _, only = ref.partition("/")
        client = self._client(server)
        out = []
        for t in client.list_tools():
            if only and t["name"] != only:
                continue
            full = f"{server}.{t['name']}"
            read_only = bool((t.get("annotations") or {}).get("readOnlyHint"))
            if not read_only and full not in step.write_tools and not only:
                continue  # whole server listed: keep only its read-only tools
            out.append(
                Tool(
                    full,
                    t.get("description") or t["name"],
                    t.get("inputSchema") or {"type": "object", "properties": {}},
                    functools.partial(_mcp_call, client, t["name"]),
                    read_only,
                )
            )
        if only and not out:
            raise ValueError(f"step '{step.id}': mcp server '{server}' has no tool '{only}'")
        return out

    def close(self) -> None:
        for client in self._mcp.values():
            client.close()
        self._mcp.clear()
