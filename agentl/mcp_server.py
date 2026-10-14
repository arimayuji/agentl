"""`agentl mcp`: an MCP server over stdio, so Claude Code, Claude Desktop, Cursor and other MCP
clients can validate, run and debug pipelines.

    {"mcpServers": {"agentl": {"command": "agentl", "args": ["mcp"]}}}

Only the standard library: JSON-RPC 2.0, one message per line. Tool output is text (JSON where it
is data). Anything a pipeline prints goes to stderr so stdout stays a clean protocol channel."""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from . import assist, graph, registry
from .mcp import PROTOCOL_VERSION
from .spec import PipelineSpec, SpecError, load
from .store import open_store

_PIPELINE = {
    "type": "string",
    "description": "path to a pipeline YAML file, or the YAML text itself",
}


def _spec(pipeline: str) -> PipelineSpec:
    if "\n" not in pipeline and Path(pipeline).is_file():
        spec = load(pipeline)
        assist.check(spec)
        return spec
    if "\n" not in pipeline:
        raise FileNotFoundError(f"no pipeline file '{pipeline}'")
    return assist.validate_text(pipeline)


class Server:
    def __init__(self, db: str | None = None):
        self.db = db
        self.tools: dict[str, tuple[dict, Callable[[dict], Any]]] = {}
        add = self._add
        add("validate", "Check a pipeline without running it.", {"pipeline": _PIPELINE}, self.validate)
        add("diagram", "The pipeline as a Mermaid diagram.", {"pipeline": _PIPELINE}, self.diagram)
        add(
            "run_pipeline",
            "Run a pipeline file and return its status, spend and how many records each step produced.",
            {
                "pipeline": {"type": "string", "description": "path to a pipeline YAML file"},
                "no_cache": {"type": "boolean", "description": "ignore cached agent answers"},
            },
            self.run_pipeline,
            read_only=False,
            required=["pipeline"],
        )
        add("list_runs", "Recent runs.", {"limit": {"type": "integer"}}, self.list_runs, required=[])
        add(
            "get_run",
            "A run with its events (the last 200).",
            {"run_id": {"type": "string"}},
            self.get_run,
        )
        add(
            "list_failures",
            "Recent failed runs, each with the failing step and its error.",
            {"limit": {"type": "integer"}},
            self.list_failures,
            required=[],
        )
        add(
            "pipeline_stats",
            "Track record per pipeline: success rate, durations, cost, failures grouped by reason "
            "and a per-day trend.",
            {
                "pipeline": {"type": "string", "description": "pipeline name; all when omitted"},
                "since": {"type": "string", "description": "how far back, e.g. 7d, 12h (default 30d)"},
            },
            self.pipeline_stats,
            required=[],
        )
        add("doctor", "Explain why a run failed.", {"run_id": {"type": "string"}}, self.doctor)
        add("list_connectors", "Installed connectors and their parameters.", {}, self.connectors)

    def _add(
        self,
        name: str,
        description: str,
        props: dict,
        fn: Callable[[dict], Any],
        read_only: bool = True,
        required: list[str] | None = None,
    ) -> None:
        schema = {
            "type": "object",
            "properties": props,
            "required": list(props) if required is None else required,
        }
        meta = {
            "name": name,
            "description": description,
            "inputSchema": schema,
            "annotations": {"readOnlyHint": read_only},
        }
        self.tools[name] = (meta, fn)

    # ------------------------------------------------------------ tools
    def validate(self, args: dict) -> str:
        spec = _spec(args["pipeline"])
        return f"ok: {spec.name} ({len(spec.steps)} steps)"

    def diagram(self, args: dict) -> str:
        return graph.mermaid(_spec(args["pipeline"]))

    def run_pipeline(self, args: dict) -> dict:
        from .runner import Runner

        spec = _spec(args["pipeline"])
        with open_store(self.db) as store:
            r = Runner(spec, store=store, use_cache=not args.get("no_cache")).run()
        return {
            "run_id": r.run_id,
            "status": r.status,
            "error": r.error,
            "records": {step: len(out) for step, out in r.outputs.items() if isinstance(out, list)},
            "cost_usd": round(r.cost_usd, 6),
            "llm_calls": r.llm_calls,
        }

    def list_runs(self, args: dict) -> list:
        with open_store(self.db) as store:
            return store.runs(int(args.get("limit") or 20))

    def get_run(self, args: dict) -> dict:
        with open_store(self.db) as store:
            run = store.run(args["run_id"])
            if run is None:
                raise KeyError(f"no run '{args['run_id']}'")
            return {**run, "events": store.events(args["run_id"])[-200:]}

    def list_failures(self, args: dict) -> list:
        with open_store(self.db) as store:
            return [
                assist.diagnose(store, r["id"]) for r in assist.failures(store, int(args.get("limit") or 10))
            ]

    def pipeline_stats(self, args: dict) -> dict:
        import time

        from . import stats
        from .checks import parse_duration

        since = time.time() - parse_duration(args.get("since") or "30d")
        with open_store(self.db) as store:
            return stats.compute(store, since, args.get("pipeline"))

    def doctor(self, args: dict) -> str:
        with open_store(self.db) as store:
            return assist.explain(assist.diagnose(store, args["run_id"]))

    def connectors(self, args: dict) -> str:
        return assist.catalog()

    # ------------------------------------------------------------ protocol
    def handle(self, msg: dict) -> dict | None:
        method, rid = msg.get("method"), msg.get("id")
        if rid is None:
            return None  # notification
        try:
            result = self._dispatch(method, msg.get("params") or {})
        except _RpcError as e:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": e.code, "message": str(e)}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def _dispatch(self, method: Any, params: dict) -> dict:
        from . import __version__

        if method == "initialize":
            return {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agentl", "version": __version__},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [meta for meta, _ in self.tools.values()]}
        if method == "tools/call":
            name = params.get("name")
            if name not in self.tools:
                raise _RpcError(-32602, f"unknown tool '{name}'")
            _, fn = self.tools[name]
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    out = fn(params.get("arguments") or {})
            except (SpecError, KeyError, ValueError, FileNotFoundError) as e:
                text = str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e)
                return {"content": [{"type": "text", "text": text}], "isError": True}
            except Exception as e:  # a tool failure is an answer for the model, not a crash
                return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
            text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str, indent=1)
            return {"content": [{"type": "text", "text": text}], "isError": False}
        raise _RpcError(-32601, f"method not found: {method}")

    def serve(self, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> None:
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        for line in stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                reply: dict | None = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "parse error"},
                }
            else:
                reply = self.handle(msg) if isinstance(msg, dict) else None
            if reply is not None:
                stdout.write(json.dumps(reply, default=str) + "\n")
                stdout.flush()


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def serve(db: str | None = None) -> None:
    registry.names()  # load connectors before the first request
    Server(db).serve()
