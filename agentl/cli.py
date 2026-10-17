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


def _checked(path: str) -> bool:
    """Lint before running (#147): on errors print the first one with its fix, so an agent fixes
    it in one turn, and do not start the run. Warnings stay with `agentl lint`."""
    from . import lint

    found = lint.check_file(path, warnings=False)
    if not found.errors:
        return True
    print(found.errors[0].text(path), file=sys.stderr)
    more = len(found.errors) - 1
    if more:
        print(f"{more} more error(s): agentl lint {path}", file=sys.stderr)
    print("the run did not start (agentl run --no-check skips this check)", file=sys.stderr)
    return False


def _mcp_install(a: argparse.Namespace) -> int:
    from . import mcp_install as mi

    try:
        if a.action == "status":
            rows = mi.status()
            if not rows:
                print("agentl is not set up in any MCP client. Run: agentl mcp install --client NAME")
            for r in rows:
                print(f"{r['client']} ({r['scope']}): {r['path']}\n  {r['command']}")
            return 0
        if not a.client:
            print(f"--client is required: one of {', '.join(mi.CLIENTS)}", file=sys.stderr)
            return 2
        t = mi.target(a.client, a.project)
        if a.action == "uninstall":
            if a.client == "claude-code" and mi.shutil.which("claude"):
                scope = "project" if a.project else "user"
                done = mi.subprocess.run(["claude", "mcp", "remove", mi.NAME, "-s", scope]).returncode == 0
            else:
                done = mi.uninstall(t)
            print(f"removed agentl from {t.path}" if done else f"agentl was not in {t.path}")
            return 0
        argv = mi.command(a.root, a.allow_code, a.db, a.uvx)
        if a.client == "claude-code" and mi.claude_cli(argv, a.project):
            print(
                "added agentl to Claude Code with `claude mcp add-json` "
                f"({'project' if a.project else 'user'} scope)"
            )
        else:
            state = mi.install(t, argv)
            backup = t.path.with_name(t.path.name + ".bak")
            note = f" (backup: {backup.name})" if state != "unchanged" and backup.exists() else ""
            print(f"{state}: agentl in {t.path}{note}")
        print("  " + " ".join(argv))
        print(mi.RESTART[a.client])
        return 0
    except mi.InstallError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _rejected(a: argparse.Namespace) -> int:
    from .runner import retry_rejected

    with open_store(a.db) as store:
        if a.action == "list":
            rows = store.rejected_list(a.pipeline, a.step, pending=not a.all)[-a.n :]
            for row in rows:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["ts"]))
                done = f"  retried by {row['retried_by']}" if row["retried_by"] else ""
                print(f"{row['id']}  {row['pipeline']}  {row['step']}  {when}  {row['error'] or ''}{done}")
            if not rows:
                print("no rejected records stored")
            return 0
        if a.action == "show":
            if not a.target or not a.target.isdigit():
                print(
                    "agentl rejected show needs a rejected record id (see agentl rejected list)",
                    file=sys.stderr,
                )
                return 2
            found = store.rejected_get(int(a.target))
            if found is None:
                print(f"no rejected record {a.target}", file=sys.stderr)
                return 1
            print(json.dumps(found, ensure_ascii=False, indent=2, default=str))
            return 0
        if not a.target:
            print("agentl rejected retry needs the pipeline file", file=sys.stderr)
            return 2
        spec = load(a.target)
        result = retry_rejected(spec, store, a.step, listeners=_listeners(a))
    if result is None:
        print(f"no pending rejected records for {spec.name}" + (f" step {a.step}" if a.step else ""))
        return 0
    return 0 if result.status == "succeeded" else 1


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
    r.add_argument(
        "--no-check", action="store_true", help="skip the lint that runs first (errors stop the run)"
    )
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

    v = cmd("validate", "check a pipeline without running it: every error, with line and fix")
    v.add_argument("pipeline")
    v.add_argument("--format", choices=["text", "json", "github"], default="text")
    v.add_argument(
        "--connect",
        action="store_true",
        help="also reach every database, URL, file and LLM key (reads no data)",
    )
    v.add_argument("--llm", action="store_true", help="with --connect: make one tiny test call per LLM")

    pl = cmd("plan", "what a run would read, call and cost, without writing or calling an LLM")
    pl.add_argument("pipeline")
    pl.add_argument("--json", action="store_true")

    li = cmd("lint", "validate plus best-practice warnings (budget, checks, secrets, unused steps)")
    li.add_argument("pipeline")
    li.add_argument("--format", choices=["text", "json", "github"], default="text")
    li.add_argument("--strict", action="store_true", help="fail on warnings too")

    cmd("schema", "print the JSON Schema of a pipeline file (editor autocomplete)")

    cmd("keygen", "print a new random key for the protect and unprotect steps")

    pv = cmd("preview", "first records into and out of a step (readers and transforms only)")
    pv.add_argument("pipeline")
    pv.add_argument("step")
    pv.add_argument("-n", type=int, default=5)

    sk = cmd("skill", "install the agentl skill for Claude Code (teaches agents to use agentl)")
    sk.add_argument("action", choices=["install", "show"])
    sk.add_argument(
        "--project", action="store_true", help="into ./.claude/skills instead of ~/.claude/skills"
    )

    d = cmd("diagram", "print the pipeline as a Mermaid diagram")
    d.add_argument("pipeline")

    cmd("runs", "list recent runs")
    co = cmd("connectors", "list installed connectors, or show one connector's parameters")
    co.add_argument("name", nargs="?", help="a connector, e.g. http.get: prints its `with:` parameters")

    n = cmd("new", "write a pipeline: from a tested template (no LLM), or from a description (asks an LLM)")
    n.add_argument("description", nargs="?", help="what the pipeline should do (asks an LLM)")
    n.add_argument("--template", "-t", metavar="NAME", help="start from a tested template, no LLM call")
    n.add_argument("--list", action="store_true", help="list the templates")
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

    se = cmd("seen", "keys skip_seen already let through: list them, or forget them to reprocess")
    se.add_argument("action", choices=["list", "forget"])
    se.add_argument("key", nargs="?", help="forget: only this key (default: the whole scope)")
    se.add_argument("--scope", help="only this scope (default scope: the pipeline name)")

    rj = cmd("rejected", "records steps rejected: list them, show one, or retry them")
    rj.add_argument("action", choices=["list", "show", "retry"])
    rj.add_argument("target", nargs="?", help="show: the rejected record id; retry: the pipeline file")
    rj.add_argument("--pipeline", help="list: only this pipeline (by name)")
    rj.add_argument("--step", help="list / retry: only this step")
    rj.add_argument("--all", action="store_true", help="list: also records already retried")
    rj.add_argument("-n", type=int, default=50, help="list: at most this many (default 50)")
    rj.add_argument(
        "--log-format", choices=["text", "json"], default=os.environ.get("AGENTL_LOG_FORMAT", "text")
    )
    rj.add_argument("--otel", action="store_true", help=argparse.SUPPRESS)
    rj.add_argument("--sentry", action="store_true", help=argparse.SUPPRESS)

    sv = cmd("serve", "run pipelines on their triggers: schedules, webhooks and new files")
    sv.add_argument("pipelines", nargs="+")
    sv.add_argument("--host", default="127.0.0.1", help="webhook address (default 127.0.0.1)")
    sv.add_argument("--port", type=int, default=7071)

    mc = cmd("mcp", "serve agentl as an MCP server over stdio, or install it in an MCP client")
    mc.add_argument(
        "action",
        nargs="?",
        default="serve",
        choices=["serve", "install", "uninstall", "status"],
        help="serve (default); install / uninstall the server in --client's config; status",
    )
    mc.add_argument(
        "--client",
        choices=["claude-desktop", "claude-code", "cursor", "vscode", "windsurf"],
        help="install/uninstall: which MCP client",
    )
    mc.add_argument("--project", action="store_true", help="install/uninstall: this folder only")
    mc.add_argument("--uvx", action="store_true", help="install: start it with `uvx agentl-etl`")
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
            if not a.no_check and not _checked(a.pipeline):
                return 2
            spec = load(a.pipeline)
            with open_store(a.db) as store:
                runner = Runner(spec, store=store, use_cache=not a.no_cache, listeners=_listeners(a))
                result = runner.run()
            if a.show:
                print(json.dumps(result.outputs.get(a.show, []), ensure_ascii=False, indent=2, default=str))
            return 0 if result.status == "succeeded" else 1
        if a.cmd in ("validate", "lint"):
            from . import lint

            found = lint.check_file(a.pipeline, warnings=a.cmd == "lint")
            failed = bool(found.errors) or (a.cmd == "lint" and a.strict and bool(found.warnings))
            if a.format != "text":
                print(found.render(a.format))
            else:
                if found.findings:
                    print(found.render("text"), file=sys.stderr if failed else sys.stdout)
                if found.spec is not None and not found.errors:
                    w = f", {len(found.warnings)} warning(s)" if found.warnings else ""
                    print(f"ok: {found.spec.name} ({len(found.spec.steps)} steps{w})")
                else:
                    print(f"{len(found.errors)} error(s)", file=sys.stderr)
            if a.cmd == "validate" and a.connect and not found.errors:
                from . import preflight

                checks = preflight.connect(a.pipeline, call_llm=a.llm)
                for c in checks:
                    mark = {"ok": "ok  ", "fail": "FAIL", "skip": "skip"}[c.status]
                    print(f"  {mark} {c.what}: {c.detail}")
                if any(c.status == "fail" for c in checks):
                    return 2
            return 2 if found.errors else 1 if failed else 0
        if a.cmd == "plan":
            from . import preflight

            with open_store(a.db) as store:
                planned = preflight.plan(load(a.pipeline), store)
            print(json.dumps(planned.to_dict(), indent=2) if a.json else planned.text())
            return 0
        if a.cmd == "preview":
            from . import preflight

            with open_store(a.db) as store:
                shown = preflight.preview(load(a.pipeline), store, a.step, a.n)
            print(json.dumps(shown, indent=2, ensure_ascii=False, default=str))
            return 0
        if a.cmd == "skill":
            source = Path(__file__).parent / "skill" / "SKILL.md"
            if a.action == "show":
                print(source.read_text(encoding="utf-8"))
                return 0
            root = Path(".claude") if a.project else Path.home() / ".claude"
            target = root / "skills" / "agentl" / "SKILL.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"installed {target}")
            return 0
        if a.cmd == "keygen":
            from .connectors.protect import new_key

            print(new_key())
            return 0
        if a.cmd == "schema":
            from . import lint

            print(json.dumps(lint.schema(), indent=2))
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
            print(registry.describe(a.name) if a.name else "\n".join(registry.names()))
            return 0
        if a.cmd == "new" and (a.list or a.template):
            from . import templates

            if a.list:
                width = max(map(len, templates.names()))
                for name in templates.names():
                    print(f"{name:<{width}}  {templates.summary(name)}")
                return 0
            text = templates.text(a.template)
            if a.output:
                with open(a.output, "x", encoding="utf-8") as f:
                    f.write(text)
                print(
                    f"wrote {a.output} from template {a.template}: "
                    f"edit paths and fields, then agentl run {a.output}",
                    file=sys.stderr,
                )
            else:
                print(text, end="")
            return 0
        if a.cmd == "new" and not a.description:
            print("agentl new: give a description, --template NAME or --list", file=sys.stderr)
            return 2
        if a.cmd == "new":
            text, spec = assist.new_pipeline(
                a.description,
                assist.llm_from_env(a.provider, a.model),
                llm_block=assist.llm_choice(a.provider, a.model),
            )
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
        if a.cmd == "seen":
            with open_store(a.db) as store:
                if a.action == "list":
                    seen = store.seen_list(a.scope)
                    for row in seen:
                        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["ts"]))
                        print(f"{row['scope']}  {row['key']}  (run {row['run_id']}, {when})")
                    if not seen:
                        print("no seen keys stored")
                    return 0
                if not a.scope:
                    print("agentl seen forget needs --scope", file=sys.stderr)
                    return 2
                count = store.seen_forget(a.scope, a.key)
                print(f"forgot {count} key(s) of {a.scope}; the next run handles them again")
                return 0
        if a.cmd == "rejected":
            return _rejected(a)
        if a.cmd == "serve":
            import logging

            from .triggers import Service

            serve_log = logging.getLogger("agentl.serve")
            if not serve_log.handlers:  # one line per run on the terminal, unless logging is set up
                handler = logging.StreamHandler()
                handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
                serve_log.addHandler(handler)
                serve_log.setLevel(logging.INFO)
            specs = [load(p) for p in a.pipelines]
            for spec in specs:
                assist.check(spec)
            with open_store(a.db) as store:
                Service(specs, store).serve(a.host, a.port)
            return 0
        if a.cmd == "mcp":
            if a.action == "serve":
                from .mcp_server import serve as serve_mcp

                serve_mcp(a.db, a.root, a.allow_code)
                return 0
            return _mcp_install(a)
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
