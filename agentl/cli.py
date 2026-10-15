"""agentl command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import assist, graph, registry
from .runner import Runner
from .spec import SpecError, load
from .store import open_store


def _print_event(event: dict) -> None:
    step = f" [{event['step']}]" if event.get("step") else ""
    data = {k: v for k, v in event["data"].items() if v not in (None, "")}
    print(f"{event['type']}{step} {json.dumps(data, default=str) if data else ''}".rstrip())


def _listeners(a: argparse.Namespace) -> list:
    from . import observability

    out: list = [observability.JsonLogs() if a.log_format == "json" else _print_event]
    if a.otel:
        out.append(observability.OpenTelemetry())
    if a.sentry:
        out.append(observability.Sentry(os.environ.get("SENTRY_DSN")))
    return out


def main(argv: list[str] | None = None) -> int:
    from . import __version__

    p = argparse.ArgumentParser(prog="agentl", description="Agentic ETL for Python")
    p.add_argument("-V", "--version", action="version", version=f"agentl {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db",
        default=None,
        help="run store: SQLite path or postgresql:// URL (default: $AGENTL_DB or .agentl/agentl.db)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def cmd(name: str, help: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help, parents=[common])

    r = cmd("run", "run a pipeline")
    r.add_argument("pipeline")
    r.add_argument("--no-cache", action="store_true")
    r.add_argument("--show", metavar="STEP", help="print the output records of a step")
    r.add_argument(
        "--log-format",
        choices=["text", "json"],
        default=os.environ.get("AGENTL_LOG_FORMAT", "text"),
        help="event output: text (default) or one JSON object per line on stderr ($AGENTL_LOG_FORMAT)",
    )
    r.add_argument(
        "--otel", action="store_true", help="emit OpenTelemetry traces and metrics (agentl-etl[otel])"
    )
    r.add_argument("--sentry", action="store_true", help="report failed runs to Sentry using $SENTRY_DSN")

    v = cmd("validate", "check a pipeline without running it")
    v.add_argument("pipeline")

    d = cmd("diagram", "print the pipeline as a Mermaid diagram")
    d.add_argument("pipeline")

    cmd("runs", "list recent runs")
    cmd("connectors", "list installed connectors")

    n = cmd("new", "write a pipeline from a description (asks an LLM, then validates)")
    n.add_argument("description")
    n.add_argument("-o", "--output", help="write the YAML here instead of printing it")
    n.add_argument("--provider", help="LLM provider (default: $AGENTL_PROVIDER or anthropic)")
    n.add_argument("--model", help="model (default: $AGENTL_MODEL)")

    dr = cmd("doctor", "explain why a run failed")
    dr.add_argument("run_id", nargs="?", help="default: the latest failed run")
    dr.add_argument("--pipeline", help="the pipeline file, sent to the LLM with --suggest")
    dr.add_argument("--suggest", action="store_true", help="ask an LLM for a fix")
    dr.add_argument("--provider")
    dr.add_argument("--model")

    st = cmd("stats", "success rate, durations, cost and failures by reason, per pipeline")
    st.add_argument("pipeline", nargs="?", help="pipeline name (default: all)")
    st.add_argument("--since", default="30d", help="how far back: 7d, 12h, 4w (default 30d)")
    st.add_argument("--json", action="store_true", help="print the report as JSON")

    sa = cmd("state", "cursors of incremental steps: show them, or reset to load from the start")
    sa.add_argument("action", choices=["show", "reset"])
    sa.add_argument("pipeline", nargs="?", help="pipeline name (show: default all; reset: required)")
    sa.add_argument("--step", help="reset only this step's cursor")

    sv = cmd("serve", "run pipelines on their triggers: schedules, webhooks and new files")
    sv.add_argument("pipelines", nargs="+")
    sv.add_argument("--host", default="127.0.0.1", help="webhook address (default 127.0.0.1)")
    sv.add_argument("--port", type=int, default=7071)

    mc = cmd("mcp", "serve agentl as an MCP server over stdio (Claude Code, Claude Desktop, Cursor)")
    mc.add_argument("--root", help="run_pipeline only runs files under this folder (default: here)")
    mc.add_argument(
        "--allow-code",
        action="store_true",
        help="let run_pipeline run pipelines with fn: steps, Python tools or local MCP servers",
    )

    ds = cmd("dashboard", "open the local dashboard")
    ds.add_argument("--port", type=int, default=7070)
    ds.add_argument("--host", default="127.0.0.1")

    a = p.parse_args(argv)
    try:
        if a.cmd == "run":
            spec = load(a.pipeline)
            with open_store(a.db) as store:
                runner = Runner(spec, store=store, use_cache=not a.no_cache, listeners=_listeners(a))
                result = runner.run()
            if a.show:
                print(json.dumps(result.outputs.get(a.show, []), ensure_ascii=False, indent=2, default=str))
            return 0 if result.status == "succeeded" else 1
        if a.cmd == "validate":
            spec = load(a.pipeline)
            assist.check(spec)
            print(f"ok: {spec.name} ({len(spec.steps)} steps)")
            return 0
        if a.cmd == "diagram":
            print(graph.mermaid(load(a.pipeline)))
            return 0
        if a.cmd == "runs":
            with open_store(a.db) as store:
                runs = store.runs()
            for run in runs:
                print(f"{run['id']}  {run['status']:<10} {run['pipeline']}  {run['error'] or ''}")
            return 0
        if a.cmd == "connectors":
            print("\n".join(registry.names()))
            return 0
        if a.cmd == "new":
            text, spec = assist.new_pipeline(a.description, assist.llm_from_env(a.provider, a.model))
            if a.output:
                with open(a.output, "x", encoding="utf-8") as f:
                    f.write(text)
                print(f"wrote {a.output}: {spec.name} ({len(spec.steps)} steps)", file=sys.stderr)
            else:
                print(text, end="")
            print("\n" + graph.mermaid(spec), file=sys.stderr)
            return 0
        if a.cmd == "doctor":
            with open_store(a.db) as store:
                run_id = a.run_id or next((r["id"] for r in assist.failures(store, 1)), None)
                if run_id is None:
                    print("no failed runs in this store")
                    return 0
                report = assist.diagnose(store, run_id)
            llm = assist.llm_from_env(a.provider, a.model) if a.suggest else None
            yaml_text = Path(a.pipeline).read_text(encoding="utf-8") if a.pipeline else None
            print(assist.explain(report, llm, yaml_text))
            return 0
        if a.cmd == "stats":
            from . import stats
            from .checks import parse_duration

            with open_store(a.db) as store:
                report = stats.compute(store, time.time() - parse_duration(a.since), a.pipeline)
            print(json.dumps(report, indent=2, default=str) if a.json else stats.text(report))
            return 0
        if a.cmd == "state":
            with open_store(a.db) as store:
                if a.action == "show":
                    rows = store.state_list(a.pipeline)
                    for row in rows:
                        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["ts"]))
                        made = f"run {row['run_id']}, {when}"
                        print(f"{row['pipeline']}  {row['step']}  {row['value']!r}  ({made})")
                    if not rows:
                        print("no cursors stored")
                    return 0
                if not a.pipeline:
                    print("agentl state reset needs a pipeline name", file=sys.stderr)
                    return 2
                count = store.state_reset(a.pipeline, a.step)
                print(f"reset {count} cursor(s) of {a.pipeline}; the next run starts from `initial`")
                return 0
        if a.cmd == "serve":
            from .triggers import Service

            specs = [load(p) for p in a.pipelines]
            for spec in specs:
                assist.check(spec)
            with open_store(a.db) as store:
                Service(specs, store).serve(a.host, a.port)
            return 0
        if a.cmd == "mcp":
            from .mcp_server import serve as serve_mcp

            serve_mcp(a.db, a.root, a.allow_code)
            return 0
        if a.cmd == "dashboard":
            from .dashboard import serve

            with open_store(a.db) as store:
                serve(store, a.host, a.port)
            return 0
    except (SpecError, KeyError, FileNotFoundError, FileExistsError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
