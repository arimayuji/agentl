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

import yaml

from . import assist, graph, registry
from .mcp import PROTOCOL_VERSION
from .spec import PipelineSpec, SpecError, _Loader, load
from .store import open_store

_PIPELINE = {
    "type": "string",
    "description": "path to a pipeline YAML file, or the YAML text itself",
}


class _Placeholders(dict):
    """Environment for validate/diagram: every ${VAR} reads as <VAR>, so a tool answer can never
    carry the value of an environment variable back to the client."""

    def __contains__(self, key: object) -> bool:
        return True

    def __getitem__(self, key: str) -> str:
        return f"<{key}>"


def _neutral(text: str) -> str:
    from .secrets import SECRET

    return SECRET.sub(lambda m: f"<{m.group(1)}>", text)


def _spec(pipeline: str, real_env: bool = False) -> PipelineSpec:
    """A pipeline from a file path or inline YAML. Only `run_pipeline` reads the real environment
    and secret references; validate and diagram see placeholders."""
    if "\n" not in pipeline and Path(pipeline).is_file():
        if real_env:
            spec = load(pipeline)
        else:
            from .spec import parse

            spec = parse(
                yaml.load(_neutral(Path(pipeline).read_text(encoding="utf-8")), Loader=_Loader),
                env=_Placeholders(),
            )
            spec.base_dir = str(Path(pipeline).resolve().parent)
        assist.check(spec)
        return spec
    if "\n" not in pipeline:
        raise FileNotFoundError(f"no pipeline file '{pipeline}'")
    if real_env:
        raise ValueError("run_pipeline takes a pipeline file, not YAML text")
    return assist.validate_text(_neutral(pipeline), env=_Placeholders())


def code_in(spec: PipelineSpec) -> list[str]:
    """What in a pipeline would run code on this machine: `fn:` steps, agent tools that are
    Python functions, and MCP servers started as local commands."""
    found = [f"step '{s.id}' runs {s.fn}" for s in spec.steps if s.fn]
    for s in spec.steps:
        for t in s.tools:
            name = t if isinstance(t, str) else next(iter(t))
            if name.startswith("fn:"):
                found.append(f"step '{s.id}' uses tool {name}")
    found += [f"mcp server '{n}' runs {m.command[0]}" for n, m in spec.mcp.items() if m.command]
    return found


class Server:
    """`root`: run_pipeline only runs files under this folder (default: the working directory).
    `allow_code`: let run_pipeline run pipelines with `fn:` steps, Python tools or local MCP
    commands; off by default, since a client steered by prompt injection could otherwise run code."""

    def __init__(self, db: str | None = None, root: str | None = None, allow_code: bool = False):
        self.db = db
        self.root = Path(root or ".").resolve()
        self.allow_code = allow_code
        self.tools: dict[str, tuple[dict, Callable[[dict], Any]]] = {}
        add = self._add
        add("validate", "Check a pipeline without running it.", {"pipeline": _PIPELINE}, self.validate)
        add("diagram", "The pipeline as a Mermaid diagram.", {"pipeline": _PIPELINE}, self.diagram)
        add(
            "lint",
            "Validate plus best-practice warnings (missing budget, delivery with no check, secrets in "
            "the YAML, unused steps). JSON findings with line, code and fix.",
            {"pipeline": _PIPELINE},
            self.lint,
        )
        file_only = {"pipeline": {"type": "string", "description": "path to a pipeline YAML file"}}
        add(
            "plan",
            "What a run would read, call and cost: records per step, cache hits, LLM calls, token "
            "and cost ranges, and what would be written. Reads the sources; never writes, never calls "
            "an LLM, never runs fn: code.",
            file_only,
            self.plan,
        )
        add(
            "preview_step",
            "The first records going into and out of one step, computed like plan (readers and "
            "transforms only). Use it to check a transform before running the pipeline.",
            {**file_only, "step": {"type": "string"}, "n": {"type": "integer", "description": "default 5"}},
            self.preview_step,
            required=["pipeline", "step"],
        )
        add(
            "list_templates",
            "Tested pipeline templates (name and what each does). Start from one with "
            "new_from_template: no LLM call, and it is known to validate and run.",
            {},
            self.list_templates,
        )
        add(
            "new_from_template",
            "Write a template to a file under the server's root (fails if the file exists unless "
            "overwrite is true). Then edit its paths and settings, and lint it.",
            {
                "name": {"type": "string", "description": "template name, from list_templates"},
                "path": {"type": "string", "description": "where to write the YAML, e.g. pipeline.yaml"},
                "overwrite": {"type": "boolean"},
            },
            self.new_from_template,
            read_only=False,
            required=["name", "path"],
        )
        add(
            "new_pipeline",
            "Paid option: write a pipeline YAML from a description with the LLM set by AGENTL_PROVIDER "
            "/ AGENTL_MODEL. Prefer list_templates + new_from_template, which cost nothing. Returns "
            "the YAML; nothing is saved.",
            {"description": {"type": "string", "description": "what the pipeline should do"}},
            self.new_pipeline,
        )
        add(
            "run_pipeline",
            "Run a pipeline file and return its status, spend and how many records each step produced. "
            "It is checked first: on an error it does not start and returns status not_started with "
            "the first error and its fix. Pipelines that run Python code (fn: steps, Python tools, "
            "local MCP servers) need the server started with --allow-code.",
            {
                "pipeline": {"type": "string", "description": "path to a pipeline YAML file"},
                "no_cache": {"type": "boolean", "description": "ignore cached agent answers"},
                "no_check": {"type": "boolean", "description": "skip the check that runs first"},
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
                "labels": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": 'only runs with all these labels, e.g. {"team": "sales-data"}',
                },
            },
            self.pipeline_stats,
            required=[],
        )
        add("doctor", "Explain why a run failed.", {"run_id": {"type": "string"}}, self.doctor)
        add(
            "repair",
            "Paid option: ask the pipeline's llm for a fix to a failed run, as a unified diff against "
            "the pipeline file, checked by applying it in memory and linting. Never changes any file; "
            "personal data and secrets in samples are masked before the llm sees them.",
            {
                "run_id": {"type": "string"},
                "pipeline": {"type": "string", "description": "path to the pipeline YAML file"},
                "llm": {"type": "string", "description": "llm name from the pipeline (default: default)"},
            },
            self.repair,
            required=["run_id", "pipeline"],
        )
        add(
            "report",
            "Draft a bug report for a failed run that looks like an agentl problem: environment, "
            "failing step, error and traceback, doctor findings, the pipeline YAML with secrets "
            "masked, a minimal repro suggestion, the issue-form markdown and a prefilled GitHub "
            "issue URL. Calls no LLM and sends nothing: show it to the user, who decides to file it. "
            "To fix it too, follow .github/CONTRIBUTING_AI.md in the agentl repository.",
            {
                "run_id": {"type": "string", "description": "default: the latest failed run"},
                "pipeline": {"type": "string", "description": "path to the pipeline YAML file"},
            },
            self.report,
            required=[],
        )
        add(
            "list_connectors",
            "Installed connectors, one line each. For one connector's with: parameters, types and "
            "defaults use describe_connector.",
            {},
            self.connectors,
        )
        add(
            "describe_connector",
            "One connector's with: parameters (types, defaults, which are required) and notes.",
            {"name": {"type": "string", "description": "e.g. csv.read, http.get, postgres.write"}},
            self.describe_connector,
        )
        add(
            "show_output",
            "The first records a step produced in a run (values cut at 200 characters), and how many "
            "it produced in all. Use it to check a result without opening files. run_id defaults to "
            "the latest run.",
            {
                "step": {"type": "string"},
                "run_id": {"type": "string"},
                "limit": {"type": "integer", "description": "default 5, at most 5"},
            },
            self.show_output,
            required=["step"],
        )

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
        """Every problem at once with line, column, code and fix, so the client can correct the
        YAML in one go. Environment values are never read (placeholders)."""
        from . import lint

        pipeline = args["pipeline"]
        if "\n" not in pipeline and Path(pipeline).is_file():
            report = lint.check_file(pipeline, env=_Placeholders())
        elif "\n" in pipeline:
            report = lint.check_text(pipeline, env=_Placeholders())
        else:
            raise FileNotFoundError(f"no pipeline file '{pipeline}'")
        if report.errors or report.spec is None:
            raise ValueError(report.render("text"))
        head = f"ok: {report.spec.name} ({len(report.spec.steps)} steps)"
        return "\n".join([head, report.render("text")]) if report.warnings else head

    def diagram(self, args: dict) -> str:
        return graph.mermaid(_spec(args["pipeline"]))

    def _path(self, args: dict, tool: str) -> Path:
        path = Path(args["pipeline"])
        if "\n" in args["pipeline"] or not path.is_file():
            raise ValueError(f"{tool} takes the path of a pipeline file, not YAML text")
        if not path.resolve().is_relative_to(self.root):
            raise ValueError(f"{tool} only runs files under {self.root}")
        return path

    def _file(self, args: dict, tool: str) -> PipelineSpec:
        """A pipeline file under --root, loaded with the real environment."""
        return _spec(str(self._path(args, tool)), real_env=True)

    def lint(self, args: dict) -> dict:
        from . import lint

        pipeline = args["pipeline"]
        if "\n" not in pipeline and Path(pipeline).is_file():
            report = lint.check_file(pipeline, env=_Placeholders())
        elif "\n" in pipeline:
            report = lint.check_text(pipeline, env=_Placeholders())
        else:
            raise FileNotFoundError(f"no pipeline file '{pipeline}'")
        return json.loads(report.render("json"))

    def plan(self, args: dict) -> dict:
        from . import preflight

        spec = self._file(args, "plan")
        with open_store(self.db) as store:
            return preflight.plan(spec, store).to_dict()

    def preview_step(self, args: dict) -> dict:
        from . import preflight

        spec = self._file(args, "preview_step")
        with open_store(self.db) as store:
            return preflight.preview(spec, store, args["step"], int(args.get("n") or 5))

    def new_pipeline(self, args: dict) -> str:
        text, _ = assist.new_pipeline(
            args["description"], assist.llm_from_env(), llm_block=assist.llm_choice()
        )
        return text

    def run_pipeline(self, args: dict) -> dict:
        from . import lint
        from .runner import Runner

        path = self._path(args, "run_pipeline")
        found = lint.check_file(str(path), warnings=False)
        if found.errors and not args.get("no_check"):  # like `agentl run` (#147): fix first, then run
            first = found.errors[0]
            return {
                "status": "not_started",
                "error": {"code": first.code, "line": first.line, "message": first.message, "fix": first.fix},
                "more_errors": len(found.errors) - 1,
            }
        spec = self._file(args, "run_pipeline")
        code = code_in(spec)
        if code and not self.allow_code:
            raise ValueError(
                "this pipeline runs code on this machine (" + "; ".join(code[:3]) + "). "
                "Run it with `agentl run`, or start the server with `agentl mcp --allow-code`."
            )
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
            filters = {str(k): str(v) for k, v in (args.get("labels") or {}).items()}
            return stats.compute(store, since, args.get("pipeline"), labels=filters)

    def doctor(self, args: dict) -> str:
        with open_store(self.db) as store:
            return assist.explain(assist.diagnose(store, args["run_id"]))

    def report(self, args: dict) -> dict:
        from . import report

        path = str(self._path(args, "report")) if args.get("pipeline") else None
        with open_store(self.db) as store:
            run_id = args.get("run_id") or next((r["id"] for r in assist.failures(store, 1)), None)
            if run_id is None:
                raise ValueError("no failed runs in this store")
            return report.build(store, run_id, path)

    def repair(self, args: dict) -> dict:
        from . import repair

        path = self._path(args, "repair")
        with open_store(self.db) as store:
            result = repair.propose(
                store, args["run_id"], path.read_text(encoding="utf-8"), str(path), args.get("llm")
            )
        result.pop("patched", None)
        return result

    def connectors(self, args: dict) -> str:
        return assist.catalog()

    def describe_connector(self, args: dict) -> str:
        name = args["name"]
        if name not in registry.names():
            import difflib

            close = difflib.get_close_matches(name, registry.names(), n=3)
            raise KeyError(
                f"no connector '{name}'" + (f"; did you mean {', '.join(close)}?" if close else "")
            )
        return registry.describe(name)

    def list_templates(self, args: dict) -> list:
        from . import templates

        return [{"name": n, "does": templates.summary(n)} for n in templates.names()]

    def new_from_template(self, args: dict) -> dict:
        from . import templates

        text = templates.text(args["name"])
        path = Path(args["path"])
        path = path if path.is_absolute() else self.root / path
        if not path.resolve().is_relative_to(self.root):
            raise ValueError(f"new_from_template only writes files under {self.root}")
        if path.exists() and not args.get("overwrite"):
            raise FileExistsError(f"{path} exists; pass overwrite: true to replace it")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return {"path": str(path), "template": args["name"], "next": "edit paths and settings, then lint"}

    def show_output(self, args: dict) -> dict:
        with open_store(self.db) as store:
            run_id = args.get("run_id")
            if not run_id:
                runs = store.runs(1)
                if not runs:
                    raise KeyError("no runs yet")
                run_id = runs[0]["id"]
            sample = store.sample(run_id, args["step"])
            if sample is None:
                raise KeyError(
                    f"no output kept for step '{args['step']}' in run {run_id} (the step did not "
                    "finish, or AGENTL_SAMPLES=0)"
                )
        limit = max(1, min(int(args.get("limit") or 5), 5))
        return {
            "run_id": run_id,
            "step": args["step"],
            "total": sample["total"],
            "records": sample["records"][:limit],
        }

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


def serve(db: str | None = None, root: str | None = None, allow_code: bool = False) -> None:
    registry.names()  # load connectors before the first request
    Server(db, root, allow_code).serve()
