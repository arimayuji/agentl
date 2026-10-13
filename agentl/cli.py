"""agentl command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import graph, registry
from .runner import Runner, add_import_paths
from .spec import SpecError, load
from .store import open_store


def _print_event(event: dict) -> None:
    step = f" [{event['step']}]" if event.get("step") else ""
    data = {k: v for k, v in event["data"].items() if v not in (None, "")}
    print(f"{event['type']}{step} {json.dumps(data, default=str) if data else ''}".rstrip())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agentl", description="Agentic ETL for Python")
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

    v = cmd("validate", "check a pipeline without running it")
    v.add_argument("pipeline")

    d = cmd("diagram", "print the pipeline as a Mermaid diagram")
    d.add_argument("pipeline")

    cmd("runs", "list recent runs")
    cmd("connectors", "list installed connectors")

    ds = cmd("dashboard", "open the local dashboard")
    ds.add_argument("--port", type=int, default=7070)
    ds.add_argument("--host", default="127.0.0.1")

    a = p.parse_args(argv)
    if getattr(a, "pipeline", None):
        add_import_paths(Path(a.pipeline))
    try:
        if a.cmd == "run":
            spec = load(a.pipeline)
            with open_store(a.db) as store:
                runner = Runner(spec, store=store, use_cache=not a.no_cache, listeners=[_print_event])
                result = runner.run()
            if a.show:
                print(json.dumps(result.outputs.get(a.show, []), ensure_ascii=False, indent=2, default=str))
            return 0 if result.status == "succeeded" else 1
        if a.cmd == "validate":
            spec = load(a.pipeline)
            graph.order(spec)
            for s in spec.steps:
                if s.use:
                    registry.get(s.use)
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
        if a.cmd == "dashboard":
            from .dashboard import serve

            with open_store(a.db) as store:
                serve(store, a.host, a.port)
            return 0
    except (SpecError, KeyError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
