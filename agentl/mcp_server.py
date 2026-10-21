"""`agentl mcp`: an MCP server, so Claude Code, Claude Desktop, Cursor and other MCP clients can
validate, run and debug pipelines. Tools, resources (templates, docs, pipeline files under
--root, runs) and prompts; over stdio, or over streamable HTTP with `agentl mcp --http :7072`.

    {"mcpServers": {"agentl": {"command": "agentl", "args": ["mcp"]}}}

Only the standard library: JSON-RPC 2.0, one message per line on stdio, one per POST on HTTP.
Tool output is text (JSON where it is data). Anything a pipeline prints goes to stderr so stdout
stays a clean protocol channel."""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import sys
import threading
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any
from urllib.parse import quote, unquote, urlsplit

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


def column_type(values: list[Any]) -> tuple[str, str | None]:
    """The type of `cast` a column reads as, and the number locale when its values tell
    (pt-BR for `1.234,56`, en-US for `1,234.56`). Values that are already typed (from JSON,
    Excel or Parquet) keep their type; text is tried against int, decimal, bool, date, datetime
    and email, in that order, and is `text` when any value fails."""
    import re

    from . import types
    from .checks import _empty

    present = [v for v in values if not _empty(v)][:500]
    if not present:
        return "text", None
    if all(isinstance(v, bool) for v in present):
        return "bool", None
    if all(isinstance(v, int) and not isinstance(v, bool) for v in present):
        return "int", None
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in present):
        return "decimal", None
    text = [str(v).strip() for v in present]
    if any(len(t) > 1 and t.startswith("0") and t.isdigit() for t in text):
        return "text", None  # leading zeros: an id or a code, not a number
    locale = None
    if any(re.fullmatch(r"-?\d{1,3}(\.\d{3})*,\d+", t) for t in text):
        locale = "pt-BR"
    elif any(re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", t) for t in text):
        locale = "en-US"

    def parses(fn: Callable[[Any], Any]) -> bool:
        try:
            return all(fn(t) is not None for t in text)
        except (ValueError, OverflowError):
            return False

    loc = locale or "en-US"
    if parses(lambda t: types.to_int(t, locale=loc)) and not any("." in t or "," in t for t in text):
        return "int", locale
    if parses(lambda t: types.to_decimal(t, locale=loc)):
        return "decimal", locale
    if parses(types.to_bool):
        return "bool", None
    try:
        order = types.date_order(text) or "dmy"
    except ValueError:
        return "text", None
    timed = any(re.search(r"\d:\d\d", t) for t in text)
    if not timed and parses(lambda t: types.to_date(t, order=order)):
        return "date", None
    if timed and parses(lambda t: types.to_datetime(t, order=order)):
        return "datetime", None
    if parses(types.to_email):
        return "email", None
    return "text", None


def _clip_values(record: dict) -> dict:
    return {k: (v[:200] + "...") if isinstance(v, str) and len(v) > 200 else v for k, v in record.items()}


_SUMMARY_COUNTS = ("written", "inserted", "updated", "unchanged", "deleted", "versioned")


def _artifacts(store: Any, run: dict) -> dict:
    """One run's outputs: records per step, and for each writer step the targets it wrote (from
    the summary records kept as the step's sample) with their row counts."""
    outputs: dict[str, int] = {}
    artifacts: list[dict] = []
    for e in store.events(run["id"]):
        if e["type"] != "step.succeeded" or not e["step"]:
            continue
        data = e["data"] or {}
        outputs[e["step"]] = data.get("records", 0)
        if "written" not in data:
            continue
        sample = store.sample(run["id"], e["step"])
        targets = [r for r in (sample or {}).get("records", []) if isinstance(r, dict) and "written" in r]
        if not targets:  # samples off (AGENTL_SAMPLES=0): only the count is known
            artifacts.append({"step": e["step"], "rows": data["written"]})
        for r in targets:
            where = {k: v for k, v in r.items() if k not in _SUMMARY_COUNTS}
            counts = {k: r[k] for k in _SUMMARY_COUNTS[1:] if isinstance(r.get(k), int)}
            artifacts.append({"step": e["step"], **where, "rows": r["written"], **counts})
    return {
        "run_id": run["id"],
        "pipeline": run["pipeline"],
        "status": run["status"],
        "started": run["started"],
        "outputs": outputs,
        "artifacts": artifacts,
    }


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

        add(
            "profile_file",
            "Shape of a local data file before writing a pipeline for it: rows, columns, the type "
            "each column reads as (int, decimal, date, datetime, bool, email or text), null rates, a "
            "sample of rows and a suggested cast: step. Reads the file under the server's root with "
            "the reader its extension picks; runs no code and calls no LLM.",
            {
                "path": {"type": "string", "description": "a data file under the root, e.g. data/orders.csv"},
                "sample": {"type": "integer", "description": "sample rows, default 5, at most 20"},
                "max_rows": {"type": "integer", "description": "rows profiled, default 10000"},
            },
            self.profile_file,
            required=["path"],
        )
        add(
            "list_artifacts",
            "What recent runs wrote: for each writer step, the file, table or other target and how "
            "many rows, plus the records every step produced. run_id picks one run; otherwise the "
            "latest runs, newest first.",
            {
                "run_id": {"type": "string"},
                "pipeline": {"type": "string", "description": "only runs of this pipeline"},
                "limit": {"type": "integer", "description": "runs, default 5, at most 50"},
            },
            self.list_artifacts,
            required=[],
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

    def profile_file(self, args: dict) -> dict:
        from . import checks, quick

        path = Path(args["path"])
        path = path if path.is_absolute() else self.root / path
        if not path.resolve().is_relative_to(self.root):
            raise ValueError(f"profile_file only reads files under {self.root}")
        if not path.is_file():
            raise FileNotFoundError(f"no file '{args['path']}'")
        records = [r if isinstance(r, dict) else {"value": r} for r in quick.read(str(path))]
        total = len(records)
        records = records[: max(1, int(args.get("max_rows") or 10000))]
        prof = checks.profile(records, sample=max(1, min(int(args.get("sample") or 5), 20)))
        from . import types

        columns: dict[str, Any] = {}
        found: dict[str, tuple[str, str | None]] = {}
        for name, col in prof["columns"].items():
            values = [r.get(name) for r in records]
            kind, locale = column_type(values)
            found[name] = (kind, locale)
            stats = {k: v for k, v in col.items() if k not in ("min", "max", "mean", "sum")}
            if kind in ("int", "decimal"):  # numbers read with the column's own locale
                loc = locale or "en-US"
                nums = [types.to_decimal(v, locale=loc) for v in values if not checks._empty(v)]
                clean = [n for n in nums if n is not None]
                low, high = min(clean), max(clean)
                if kind == "int":
                    low, high = int(low), int(high)
                stats.update(min=low, max=high, mean=round(sum(clean) / len(clean), 4))
                stats.pop("top", None)
            elif "top" not in stats:
                seen = Counter(str(v) for v in values if not checks._empty(v))
                stats["top"] = [[v, n] for v, n in seen.most_common(5)]
            columns[name] = {
                "type": kind,
                "null_rate": round(col["nulls"] / len(records), 4) if records else 0.0,
                **stats,
            }
        locales = {loc for _, loc in found.values() if loc}
        one = locales.pop() if len(locales) == 1 else None
        fields: dict[str, Any] = {
            name: kind if not loc or loc == one else {"type": kind, "locale": loc}
            for name, (kind, loc) in found.items()
            if kind != "text"
        }
        cast: dict[str, Any] = {**({"locale": one} if one else {}), "fields": fields}
        return {
            "path": path.resolve().relative_to(self.root).as_posix(),
            "rows": total,
            "profiled_rows": len(records),
            "columns": columns,
            "sample": [_clip_values(r) for r in prof["sample"]],
            "suggested_cast": {"use": "cast", "with": cast} if fields else None,
        }

    def list_artifacts(self, args: dict) -> list:
        with open_store(self.db) as store:
            if args.get("run_id"):
                run = store.run(args["run_id"])
                if run is None:
                    raise KeyError(f"no run '{args['run_id']}'")
                runs = [run]
            else:
                limit = max(1, min(int(args.get("limit") or 5), 50))
                pipeline = args.get("pipeline")
                runs = store.runs(limit * 20 if pipeline else limit)
                runs = [r for r in runs if not pipeline or r["pipeline"] == pipeline][:limit]
            return [_artifacts(store, r) for r in runs]

    # ------------------------------------------------------------ resources
    def _pipeline_files(self, limit: int = 200) -> list[Path]:
        """YAML files under --root that look like pipelines (a top-level `steps:`), skipping
        hidden folders and virtual environments."""
        found: list[Path] = []
        for path in sorted(self.root.rglob("*.y*ml")):
            rel = path.relative_to(self.root).parts
            if any(p.startswith(".") or p in ("node_modules", "site", "venv") for p in rel[:-1]):
                continue
            if path.suffix not in (".yaml", ".yml") or not path.is_file():
                continue
            try:
                head = path.read_text(encoding="utf-8", errors="replace")[:20000]
            except OSError:
                continue
            if any(line.startswith("steps:") for line in head.splitlines()):
                found.append(path)
                if len(found) >= limit:
                    break
        return found

    def list_resources(self) -> list[dict]:
        from . import templates

        out = [
            _res(
                "agentl://docs/skill",
                "How to build, check and run pipelines (the agent guide).",
                "text/markdown",
            ),
            _res("agentl://docs/connectors", "Installed connectors, one line each.", "text/plain"),
            _res("agentl://docs/schema", "JSON Schema of a pipeline file.", "application/json"),
            _res("agentl://runs", "The 20 latest runs.", "application/json"),
        ]
        out += [
            _res(f"agentl://templates/{n}", templates.summary(n), "application/yaml", name=n)
            for n in templates.names()
        ]
        out += [
            _res(
                "agentl://pipelines/" + quote(p.relative_to(self.root).as_posix()),
                "Pipeline file under the server's root.",
                "application/yaml",
                name=p.relative_to(self.root).as_posix(),
            )
            for p in self._pipeline_files()
        ]
        return out

    def resource_templates(self) -> list[dict]:
        return [
            {
                "uriTemplate": "agentl://runs/{run_id}",
                "name": "run",
                "description": "A run's summary: status, failing step, error, rejected records.",
                "mimeType": "application/json",
            },
            {
                "uriTemplate": "agentl://connectors/{name}",
                "name": "connector",
                "description": "One connector's with: parameters and notes, e.g. csv.read.",
                "mimeType": "text/plain",
            },
            {
                "uriTemplate": "agentl://pipelines/{path}",
                "name": "pipeline",
                "description": "A pipeline YAML file under the server's root.",
                "mimeType": "application/yaml",
            },
        ]

    def read_resource(self, uri: str) -> dict:
        """One resource as {uri, mimeType, text}. KeyError for an unknown URI; ValueError for a
        pipeline path outside --root."""
        from . import lint, templates

        if not uri.startswith("agentl://"):
            raise KeyError(f"unknown resource '{uri}'")
        kind, _, rest = uri[len("agentl://") :].partition("/")
        rest = unquote(rest)
        if kind == "docs" and rest == "skill":
            text = (Path(__file__).parent / "skill" / "SKILL.md").read_text(encoding="utf-8")
            return _content(uri, "text/markdown", text)
        if kind == "docs" and rest == "connectors":
            return _content(uri, "text/plain", assist.catalog())
        if kind == "docs" and rest == "schema":
            return _content(uri, "application/json", json.dumps(lint.schema(), indent=1))
        if kind == "templates" and rest:
            try:
                return _content(uri, "application/yaml", templates.text(rest))
            except ValueError as e:
                raise KeyError(str(e)) from None
        if kind == "connectors" and rest:
            return _content(uri, "text/plain", self.describe_connector({"name": rest}))
        if kind == "runs":
            with open_store(self.db) as store:
                data: Any = assist.diagnose(store, rest) if rest else store.runs(20)
            return _content(
                uri, "application/json", json.dumps(data, ensure_ascii=False, default=str, indent=1)
            )
        if kind == "pipelines" and rest:
            path = (self.root / rest).resolve()
            if not path.is_relative_to(self.root):
                raise ValueError(f"pipeline resources are only files under {self.root}")
            if path.suffix not in (".yaml", ".yml") or not path.is_file():
                raise KeyError(f"no pipeline file '{rest}' under {self.root}")
            return _content(uri, "application/yaml", _neutral(path.read_text(encoding="utf-8")))
        raise KeyError(f"unknown resource '{uri}'")

    # ------------------------------------------------------------ prompts
    def get_prompt(self, name: Any, args: dict) -> dict:
        if name not in PROMPTS:
            raise _RpcError(-32602, f"unknown prompt '{name}'")
        meta, render = PROMPTS[name]
        missing = [a["name"] for a in meta["arguments"] if a.get("required") and not args.get(a["name"])]
        if missing:
            raise _RpcError(-32602, f"prompt '{name}' needs: {', '.join(missing)}")
        text = render({k: str(v) for k, v in args.items()})
        return {
            "description": meta["description"],
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
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
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "agentl", "version": __version__},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [meta for meta, _ in self.tools.values()]}
        if method == "resources/list":
            return {"resources": self.list_resources()}
        if method == "resources/templates/list":
            return {"resourceTemplates": self.resource_templates()}
        if method == "resources/read":
            uri = str(params.get("uri") or "")
            try:
                return {"contents": [self.read_resource(uri)]}
            except KeyError as e:
                raise _RpcError(-32002, str(e.args[0]) if e.args else uri) from None
            except (ValueError, SpecError) as e:
                raise _RpcError(-32602, str(e)) from None
        if method == "prompts/list":
            return {"prompts": [meta for meta, _ in PROMPTS.values()]}
        if method == "prompts/get":
            return self.get_prompt(params.get("name"), params.get("arguments") or {})
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


def _res(uri: str, description: str, mime: str, name: str | None = None) -> dict:
    return {"uri": uri, "name": name or uri.rsplit("/", 1)[-1], "description": description, "mimeType": mime}


def _content(uri: str, mime: str, text: str) -> dict:
    return {"uri": uri, "mimeType": mime, "text": text}


def _arg(name: str, description: str, required: bool = False) -> dict:
    return {"name": name, "description": description, "required": required}


def _build_prompt(a: dict) -> str:
    return (
        f"Build an agentl pipeline that reads from: {a['sources']}\n"
        f"and delivers to: {a['destination']}\n"
        + (f"Also: {a['notes']}\n" if a.get("notes") else "")
        + "\nSteps: call list_templates and pick the closest template (read it with the "
        "agentl://templates/<name> resource); write it with new_from_template; adapt paths and "
        "settings, using describe_connector for each connector's with: parameters; run lint until "
        "it has no errors; then plan to show records, LLM calls and cost before anything runs. "
        "Ask me before run_pipeline. Prefer templates over new_pipeline, which calls a paid LLM."
    )


def _explain_prompt(a: dict) -> str:
    which = f"run {a['run_id']}" if a.get("run_id") else "the latest failed run (list_failures, limit 1)"
    return (
        f"Explain why {which} failed. Use doctor and get_run (or the agentl://runs/<run_id> "
        "resource), and show_output for the steps before the failure. Answer in a few lines: the "
        "likely cause, then the concrete fix (a YAML change, an environment variable, a "
        "permission). Say when the record is not enough to tell. Do not rerun the pipeline."
    )


def _cost_prompt(a: dict) -> str:
    return (
        f"Estimate what running the pipeline {a['pipeline']} would cost, without running it. Call "
        "plan on it and report, per step: records in and out, cache hits, LLM calls, the token and "
        "cost range, and what would be written. Then the total, and the settings that would lower "
        "it most (a cheaper model, a budget, a filter before the LLM step)."
    )


PROMPTS: dict[str, tuple[dict, Callable[[dict], str]]] = {
    "build_pipeline": (
        {
            "name": "build_pipeline",
            "title": "Build a pipeline",
            "description": "Build a pipeline from the tested templates, for given sources and destination.",
            "arguments": [
                _arg("sources", "where the data comes from, e.g. a CSV folder, an API", True),
                _arg("destination", "where it goes, e.g. a Postgres table, a sheet", True),
                _arg("notes", "anything else: transforms, schedule, checks"),
            ],
        },
        _build_prompt,
    ),
    "explain_failed_run": (
        {
            "name": "explain_failed_run",
            "title": "Explain the last failed run",
            "description": "Find the cause of a failed run and the fix.",
            "arguments": [_arg("run_id", "default: the latest failed run")],
        },
        _explain_prompt,
    ),
    "estimate_cost": (
        {
            "name": "estimate_cost",
            "title": "Estimate the cost before running",
            "description": "What a run would read, call and cost, from plan, without running it.",
            "arguments": [_arg("pipeline", "path to the pipeline YAML file", True)],
        },
        _cost_prompt,
    ),
}


# ---------------------------------------------------------------- streamable HTTP
TOKEN_ENV = "AGENTL_MCP_TOKEN"
_LOCAL = {"127.0.0.1", "localhost", "::1"}
_MAX_BODY = 10 * 1024 * 1024


def parse_bind(value: str) -> tuple[str, int]:
    """`:7072`, `7072`, `0.0.0.0:7072` or `[::1]:7072` -> (host, port); host defaults to 127.0.0.1."""
    host, sep, port = value.rpartition(":")
    if not sep:
        host, port = "", value
    host = host.strip("[]") or "127.0.0.1"
    if not port.isdigit():
        raise ValueError(f"--http takes [HOST]:PORT, e.g. :7072, not '{value}'")
    return host, int(port)


def http_server(server: Server, host: str = "127.0.0.1", port: int = 7072, token: str | None = None) -> Any:
    """A streamable HTTP endpoint at /mcp (MCP 2025-06-18): POST one JSON-RPC message, get one JSON
    answer (202 for notifications). No server-initiated stream, so GET and DELETE answer 405. With
    `token`, every request needs `Authorization: Bearer <token>`. Browser requests from other
    origins are refused (DNS rebinding). Returns the bound ThreadingHTTPServer; call serve_forever."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    lock = threading.Lock()  # tools share one store and redirect stdout: one call at a time

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "agentl-mcp"

        def log_message(self, format: str, *args: Any) -> None:
            sys.stderr.write("agentl mcp: " + (format % args) + "\n")

        def _send(self, status: int, body: dict | None = None, headers: dict | None = None) -> None:
            data = json.dumps(body, default=str).encode() if body is not None else b""
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _error(self, status: int, code: int, message: str, headers: dict | None = None) -> None:
            self._send(
                status, {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}, headers
            )

        def _allowed(self) -> bool:
            if urlsplit(self.path).path != "/mcp":
                self._error(404, -32601, "not found: the MCP endpoint is /mcp")
                return False
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).hostname not in _LOCAL:
                self._error(403, -32600, f"origin {origin} not allowed")
                return False
            if token is not None:
                got = self.headers.get("Authorization", "")
                if not hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
                    self._error(401, -32001, "missing or wrong bearer token", {"WWW-Authenticate": "Bearer"})
                    return False
            version = self.headers.get("MCP-Protocol-Version")
            if version and version not in _VERSIONS:
                self._error(400, -32600, f"unsupported MCP-Protocol-Version {version}")
                return False
            return True

        def do_POST(self) -> None:
            if not self._allowed():
                return
            try:
                size = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                size = -1
            if size < 0 or size > _MAX_BODY:
                self._error(413 if size > 0 else 400, -32600, "bad or too large Content-Length")
                return
            try:
                msg = json.loads(self.rfile.read(size) or b"null")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._error(400, -32700, "parse error")
                return
            if not isinstance(msg, dict):
                self._error(400, -32600, "one JSON-RPC message per request")
                return
            if "method" not in msg:  # a response or notification from the client
                self._send(202)
                return
            with lock:
                reply = server.handle(msg)
            if reply is None:
                self._send(202)
            else:
                self._send(200, reply)

        def _not_allowed(self) -> None:
            if self._allowed():
                self._send(405, None, {"Allow": "POST"})

        do_GET = do_DELETE = _not_allowed

    return ThreadingHTTPServer((host, port), Handler)


_VERSIONS = {PROTOCOL_VERSION, "2025-03-26"}


def serve(
    db: str | None = None,
    root: str | None = None,
    allow_code: bool = False,
    http: str | None = None,
    token_env: str = TOKEN_ENV,
) -> None:
    registry.names()  # load connectors before the first request
    server = Server(db, root, allow_code)
    if http is None:
        server.serve()
        return
    host, port = parse_bind(http)
    token = os.environ.get(token_env) or None
    if token is None and host not in _LOCAL:
        raise SystemExit(
            f"agentl mcp: refusing to listen on {host} without a token; set {token_env} "
            "(clients send it as Authorization: Bearer ...)"
        )
    httpd = http_server(server, host, port, token)
    auth = f"bearer token from {token_env}" if token else f"no token ({token_env} not set)"
    print(f"agentl mcp: streamable HTTP on http://{http_host(host)}:{port}/mcp, {auth}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def http_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host
