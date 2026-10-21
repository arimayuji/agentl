"""agentl command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import assist, graph, registry
from .labels import parse_filters
from .runner import Runner
from .spec import PipelineSpec, SpecError, load
from .store import open_store

LABEL_HELP = "only runs with this label, key=value (repeatable)"


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


def _resolve_decisions(a: argparse.Namespace) -> int:
    """Decisions are {normalised value: reference id or null}; edit them and import them back."""
    from .resolve import decisions_key

    key = decisions_key(a.pipeline, a.step)
    with open_store(a.db) as store:
        if a.action == "export":
            text = json.dumps(store.cache_get(key) or {}, indent=2, sort_keys=True, ensure_ascii=False)
            if a.file:
                Path(a.file).write_text(text + "\n", encoding="utf-8")
            else:
                print(text)
            return 0
        if not a.file:
            print("agentl resolve import needs a JSON file", file=sys.stderr)
            return 2
        data = json.loads(Path(a.file).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(v is None or isinstance(v, str) for v in data.values()):
            print("the file must map each value to a reference id or null", file=sys.stderr)
            return 2
        store.cache_set(key, data)
        print(f"imported {len(data)} decision(s) for {a.pipeline} / {a.step}")
        return 0


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
    return _exit_code(spec, result)


def _review(a: argparse.Namespace) -> int:
    from . import review

    with open_store(a.db) as store:
        if a.action == "list":
            status = None if a.status == "all" else a.status
            rows = store.review_list(a.pipeline, a.step, status=status)[-a.n :]
            for row in rows:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["ts"]))
                answer = json.dumps(row["value"] or row["answer"], ensure_ascii=False, default=str)
                print(
                    f"{row['id']}  {row['pipeline']}  {row['step']}  {when}  {row['status']}"
                    f"  {row['reason']}  {answer}"
                )
            if not rows:
                print("no answers waiting for review" if status == "pending" else "no reviewed answers")
            return 0
        if a.action == "export":
            if not a.golden:
                print("agentl review export needs --golden FILE", file=sys.stderr)
                return 2
            added = review.export_golden(store, a.golden, a.pipeline, a.step)
            print(f"added {added} golden record(s) to {a.golden}")
            return 0
        ids = a.args if a.action != "fix" else a.args[:1]
        if not ids or not all(i.isdigit() for i in ids):
            print(f"agentl review {a.action} needs a review id (see agentl review list)", file=sys.stderr)
            return 2
        try:
            if a.action == "show":
                found = store.review_get(int(ids[0]))
                if found is None:
                    raise KeyError(ids[0])
                print(json.dumps(found, ensure_ascii=False, indent=2, default=str))
            elif a.action == "fix":
                value = review.fix(store, int(ids[0]), review.parse_assignments(a.args[1:], a.value))
                print(f"fixed {ids[0]}: {json.dumps(value, ensure_ascii=False, default=str)}")
            else:
                for i in ids:
                    if a.action == "accept":
                        review.accept(store, int(i))
                    else:
                        review.reject(store, int(i))
                print(f"{a.action}ed {len(ids)} answer(s); the next run reuses the decision")
        except KeyError as e:
            print(f"no review item {e.args[0]}", file=sys.stderr)
            return 1
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        return 0


def _exit_code(spec: Any, result: Any) -> int:
    """Per outcome, from `outcomes.exit_codes` (default: 1 for failed, 0 otherwise)."""
    if result.outcome is None:
        return 0 if result.status in ("succeeded", "skipped", "no_change") else 1
    return int(spec.outcomes.exit_code(result.outcome))


def _bug_hint(store: Any, spec: Any, result: Any) -> None:
    """One line after a failed run whose error looks like an agentl bug (not a config error)."""
    from . import report

    if result.status != "failed":
        return
    found = assist.diagnose(store, result.run_id)
    step = next((s for s in spec.steps if s.id == found["failed_step"]), None)
    error = (found.get("failure") or {}).get("error") or found.get("error")
    mode = spec.report.on_bug
    if mode == "off" or not report.likely_bug(error, "fn" if step is not None and step.fn else None):
        return
    line = report.hint(result.run_id)
    if line is None:
        return
    print(line, file=sys.stderr)
    if mode == "open":
        import webbrowser

        url = (
            report.build(store, result.run_id)["url"]
            or f"{report.REPO}/issues/new?template={report.TEMPLATE}"
        )
        print(f"Opening the prefilled issue form (nothing is submitted): {url}", file=sys.stderr)
        webbrowser.open(url)


def _confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _report(a: argparse.Namespace) -> int:
    from . import report

    crash = None
    run_id = a.run_id
    if a.crash:
        path = Path(report.CRASH_FILE)
        if not path.is_file():
            print(f"no saved crash ({report.CRASH_FILE})", file=sys.stderr)
            return 2
        crash = path.read_text(encoding="utf-8")
        built = report.build(None, None, a.pipeline, crash)
    else:
        with open_store(a.db) as store:
            run_id = run_id or next((r["id"] for r in assist.failures(store, 1)), None)
            if run_id is None:
                print("no failed runs in this store (use --crash for a CLI crash)", file=sys.stderr)
                return 2
            built = report.build(store, run_id, a.pipeline)
    if a.output:
        Path(a.output).write_text(built["markdown"], encoding="utf-8")
    if a.json:
        print(json.dumps(built, indent=2, default=str))
    else:
        print(report.text(built, a.output))
    if a.submit:
        result = report.submit(built, _confirm)
        print(report.submit_text(result))
        return 1 if result["status"] == "failed" else 0
    return 0


def _gaps(a: argparse.Namespace) -> int:
    from . import gaps

    spec = gaps.load_spec(a.pipeline, _params(a.param))
    if spec.expect is None:
        print(f"{a.pipeline}: no expect: block (expect.partitions and expect.present_in)", file=sys.stderr)
        return 2
    found = gaps.find(spec.expect, a.since, a.until, spec.base_dir)
    fmt = spec.expect.partitions.fmt
    cmds = gaps.backfill_commands(a.pipeline, found, fmt)
    if a.notify and found.missing:
        _gaps_notify(spec, found, a.notify)
    if a.fill:

        def progress(item: str, status: str, err: str | None) -> None:
            print(f"{status} {item}" + (f": {err}" if err else ""), file=sys.stderr)

        filled = gaps.fill(
            a.pipeline,
            found,
            fmt,
            max_runs=a.max,
            param=a.param_name,
            params=_params(a.param),
            db=a.db,
            concurrency=a.concurrency,
            on_done=progress,
        )
        if a.json:
            print(json.dumps({**found.to_dict(), "fill": filled.to_dict()}, indent=2, ensure_ascii=False))
        else:
            print(gaps.summary(spec.name, found))
            for r in filled.reports:
                print(r.text())
            if filled.deferred:
                print(f"{len(filled.deferred)} partition(s) over --max {a.max}: run again to continue")
        return 0 if filled.ok and not (a.fail and filled.deferred) else 1
    if a.json:
        print(json.dumps({**found.to_dict(), "commands": cmds}, indent=2, ensure_ascii=False))
    else:
        print(gaps.summary(spec.name, found))
        n = len(found.missing)
        if a.commands:
            if cmds:
                print("\nbackfill:")
                for c in cmds:
                    print(f"  {c}")
            elif n:
                print("\n(no backfill lines: only daily partitions in %Y-%m-%d map to --from/--to)")
    return 1 if a.fail and found.missing else 0


def _digest(a: argparse.Namespace) -> int:
    from . import digest

    spec = load(a.pipeline, params=_params(a.param))
    if not (spec.notify and spec.notify.digest):
        print(f"{a.pipeline}: no notify.digest block", file=sys.stderr)
        return 2
    with open_store(a.db) as store:
        if a.show:
            print(digest.show(spec, store, only_errors=a.errors))
            return 0
        outcomes = digest.send_due(spec, store, force=a.force, only_errors=a.errors)
    for channel, outcome in outcomes.items():
        print(f"{channel}: {outcome}")
    return 1 if any(o.startswith("failed") for o in outcomes.values()) else 0


def _gaps_notify(spec: Any, found: Any, names: list[str]) -> None:
    """Send the gaps summary to channels declared under the pipeline's `notify.channels`."""
    from . import gaps, notify

    channels = spec.notify.channels if spec.notify else {}
    event = {"type": "gaps.found", "pipeline": spec.name, "data": found.to_dict()}
    text = gaps.summary(spec.name, found)
    for name in names:
        if name not in channels:
            raise ValueError(
                f"--notify {name}: no such channel under notify.channels ({', '.join(channels)})"
            )
        try:
            notify.send(name, channels[name], text, event)
        except Exception as e:  # a broken channel must not hide the gaps or stop --fill
            print(f"notify {name} failed: {e}", file=sys.stderr)


def _snapshot(a: argparse.Namespace) -> int:
    from . import snapshots

    spec = load(a.pipeline, params=_params(a.param))
    with open_store(a.db) as store:
        result = Runner(spec, store=store, use_cache=not a.no_cache, listeners=[]).run()
    if result.status != "succeeded":
        print(f"run {result.status}: snapshots not compared", file=sys.stderr)
        return 1
    folder = Path(a.dir) / spec.name
    key = [k.strip() for k in a.key.split(",") if k.strip()] if a.key else None
    steps = a.step or [s.id for s in spec.steps]
    pending: list[str] = []
    for step in steps:
        new = list(result.outputs.get(step, []))
        old = snapshots.load_snapshot(folder, step)
        if old is None:
            print(f"{step}: no snapshot ({len(new)} records)")
            pending.append(step)
            continue
        d = snapshots.diff_records(old, new, key)
        if d.empty:
            print(f"{step}: ok ({len(new)} records)")
        else:
            print(d.render(step))
            pending.append(step)
    if not pending:
        return 0
    if not a.update_snapshots:
        print(f"{len(pending)} step(s) differ; accept with --update-snapshots", file=sys.stderr)
        return 1
    if not a.yes:
        if not sys.stdin.isatty():
            print("not a terminal: pass --yes to accept", file=sys.stderr)
            return 1
        if input(f"accept {len(pending)} snapshot(s)? [y/N] ").strip().lower() not in ("y", "yes"):
            return 1
    for step in pending:
        print(f"wrote {snapshots.save_snapshot(folder, step, result.outputs.get(step, []), key)}")
    return 0


def _windowed(
    a: argparse.Namespace, spec: Any, start: Any = None, end: Any = None, resume: bool = True
) -> int:
    from .windows import run_windows

    with open_store(a.db) as store:
        report = run_windows(
            spec, store, start, end, resume=resume, use_cache=not a.no_cache, listeners=_listeners(a)
        )
    if getattr(a, "show", None):
        rows = [r for res in report.results.values() for r in res.outputs.get(a.show, [])]
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    print(f"windows: {len(report.done)} done, {len(report.skipped)} skipped, {len(report.failed)} failed")
    for key, err in report.failed.items():
        print(f"  pending {key}: {err}", file=sys.stderr)
    return 0 if report.status == "succeeded" else 1


def _has_window(path: str) -> bool:
    import yaml

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return isinstance(raw, dict) and raw.get("window") is not None


def _window_backfill(a: argparse.Namespace) -> int:
    import datetime as dt

    if a.glob or a.param_name or a.every != "day" or a.concurrency != 1:
        raise ValueError(
            "a pipeline with `window:` is backfilled by date range only: "
            "--glob, --param-name, --every and --concurrency do not apply"
        )
    if not _checked(a.pipeline):
        return 2
    spec = load(a.pipeline, params=_params(a.param))
    end = dt.date.fromisoformat(a.end) + dt.timedelta(days=1)  # --to is inclusive
    return _windowed(a, spec, dt.date.fromisoformat(a.start), end, resume=not a.redo)


def _schedule_next(spec: PipelineSpec, n: int) -> int:
    """Print the next firings of every cron and schedule trigger, in their own time zones."""
    import datetime as dt

    from .schedule import Schedule
    from .triggers import Cron, _now

    found = False
    for i, t in enumerate(spec.trigger):
        if t.schedule:
            found = True
            sch = Schedule(t.schedule)
            print(f"trigger {i} (schedule, {t.schedule.tz}):")
            for d, when in sch.next_firings(dt.datetime.now(dt.timezone.utc), n):
                first = " (expected)" if when == sch.start_of(d) else ""
                print(f"  {when.isoformat()}  cycle {d.isoformat()}{first}")
        elif t.cron:
            found = True
            print(f"trigger {i} (cron {t.cron}, {t.timezone or 'server local time'}):")
            when = _now(t.timezone)
            for _ in range(n):
                when = Cron(t.cron).next_after(when)
                print(f"  {when.isoformat()}")
    if not found:
        print(f"{spec.name} has no cron or schedule trigger", file=sys.stderr)
        return 1
    return 0


def _mappings(a: argparse.Namespace) -> int:
    """`agentl mappings`: print stored mappings as YAML ready to pin, approve or forget them."""
    with open_store(a.db) as store:
        if a.action == "show":
            rows = store.mapping_list(a.pipeline)
            for row in rows:
                for sig, m in row.get("layouts", {}).items():
                    state = "approved" if m.get("approved") else "waiting for review"
                    llm = f", by LLM: {', '.join(m['by_llm'])}" if m.get("by_llm") else ""
                    print(f"# {row['pipeline']}  {row['step']}  layout {sig}  ({state}{llm})")
                    print("mapping:")
                    for f, c in m.get("columns", {}).items():
                        print(f"  {f}: {json.dumps(c, ensure_ascii=False)}")
            if not rows:
                print("no mappings stored")
            return 0
        if not (a.pipeline and a.step):
            print(f"agentl mappings {a.action} needs a pipeline name and a step", file=sys.stderr)
            return 2
        stored = store.mapping_get(a.pipeline, a.step) or {}
        layouts = dict(stored.get("layouts") or {})
        chosen = [s for s in layouts if a.layout in (None, s)]
        if not chosen:
            print(f"no mapping stored for {a.pipeline} {a.step}", file=sys.stderr)
            return 1
        for sig in chosen:
            if a.action == "approve":
                layouts[sig] = {**layouts[sig], "approved": True}
            else:
                del layouts[sig]
        store.mapping_set(a.pipeline, a.step, {"layouts": layouts})
        done = "approved" if a.action == "approve" else "forgot"
        print(f"{done} {len(chosen)} layout(s) of {a.pipeline} {a.step}")
        return 0


def _params(pairs: list[str] | None) -> dict[str, str]:
    """`--param key=value`, repeatable. Values stay strings; the pipeline's types convert them."""
    out = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--param expects key=value, got {pair!r}")
        out[key.strip()] = value
    return out


def _declared(path: str) -> set[str]:
    import yaml

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return set((raw or {}).get("params") or {}) if isinstance(raw, dict) else set()


def _param_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--param",
        action="append",
        metavar="KEY=VALUE",
        help="run parameter declared under params: (repeatable)",
    )


def _fixtures(args: list[str]) -> dict[str | None, str]:
    """`--fixture [STEP=]PATH` values: a path that exists is taken whole, even with an `=` in it."""
    fixtures: dict[str | None, str] = {}
    for f in args:
        sid, sep, path = f.partition("=")
        if sep and sid and not Path(f).exists():
            fixtures[sid] = path
        else:
            fixtures[None] = f
    return fixtures


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
    _param_arg(r)
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

    rs = cmd("resume", "resume a failed run: skip the steps that completed, sinks included")
    rs.add_argument("run_id")
    rs.add_argument("pipeline", help="the pipeline file of that run (its spec must be unchanged)")
    rs.add_argument("--no-cache", action="store_true")
    _param_arg(rs)
    rs.add_argument(
        "--log-format", choices=["text", "json"], default=os.environ.get("AGENTL_LOG_FORMAT", "text")
    )
    rs.add_argument("--otel", action="store_true", help=argparse.SUPPRESS)
    rs.add_argument("--sentry", action="store_true", help=argparse.SUPPRESS)

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

    te = cmd("test", "run a pipeline on fixture files (echo LLM, no writes) and compare with a golden file")
    te.add_argument("pipeline")
    te.add_argument(
        "--fixture",
        action="append",
        default=[],
        metavar="[STEP=]PATH",
        help="file a source step reads instead (STEP= may be left out with one source step)",
    )
    te.add_argument("--expect", required=True, help="golden JSON: [records] or {writer step: [records]}")
    te.add_argument("--key", help="match rows by this field instead of by position")
    te.add_argument(
        "--ignore",
        action="append",
        default=[],
        metavar="FIELD",
        help="leave this field out of the comparison",
    )
    te.add_argument("--update", action="store_true", help="write the golden file from this run")

    df = cmd("diff", "what a run would insert, update and delete in each database writer, without writing")
    df.add_argument("pipeline")
    df.add_argument(
        "--fixture",
        action="append",
        default=[],
        metavar="[STEP=]PATH",
        help="file a source step reads instead (STEP= may be left out with one source step)",
    )
    df.add_argument("--echo", action="store_true", help="answer agent steps with the offline echo provider")
    df.add_argument("--exit-code", action="store_true", help="exit 1 when something would change")
    df.add_argument("--limit", type=int, default=10, help="changed rows shown per writer (default 10)")
    df.add_argument("--json", action="store_true")

    pv = cmd("preview", "first records into and out of a step (readers and transforms only)")
    pv.add_argument("pipeline")
    pv.add_argument("step")
    pv.add_argument("-n", type=int, default=5)

    sk = cmd("skill", "install the agentl skill for Claude Code (teaches agents to use agentl)")
    sk.add_argument("action", choices=["install", "show"])
    sk.add_argument(
        "--project", action="store_true", help="into ./.claude/skills instead of ~/.claude/skills"
    )

    ev = cmd("eval", "score agent steps on their golden sets: accuracy per field, cost, differences")
    ev.add_argument("pipeline", help="the pipeline, or `add` (then PIPELINE STEP --from-run RUN_ID)")
    ev.add_argument("step", nargs="?", help="one step (default: every step with a golden set)")
    ev.add_argument("add_step", nargs="?", help=argparse.SUPPRESS)
    ev.add_argument(
        "--compare",
        action="append",
        default=[],
        metavar="model=NAME",
        help="also run with another model (or provider=NAME, or prompt@GIT_REF: the step's prompt "
        "at that git ref), side by side; repeatable",
    )
    ev.add_argument(
        "--from-run",
        metavar="RUN_ID",
        help="add: append the step's records of this run, with the answers it gave, to its golden file",
    )
    ev.add_argument("--json", action="store_true", help="print the reports as JSON")

    d = cmd("diagram", "print the pipeline as a Mermaid diagram")
    d.add_argument("pipeline")

    ru = cmd("runs", "list recent runs, or show what one run did (rows, changes, LLM cost)")
    ru.add_argument("action", nargs="?", choices=["list", "show"], default="list")
    ru.add_argument("run_id", nargs="?", help="show: the run id")
    ru.add_argument("--json", action="store_true", help="show: print the ledger as JSON")
    ru.add_argument(
        "--label",
        action="append",
        metavar="KEY=VALUE",
        help="only runs with this label, key=value (repeatable)",
    )

    ct = cmd("cost", "LLM calls, tokens and cost over recent runs, grouped by pipeline, step, model or day")
    ct.add_argument("--since", default="30d", help="how far back: 7d, 12h, 4w (default 30d)")
    ct.add_argument("--by", default="pipeline,model", help="comma list of pipeline, step, model, day")
    ct.add_argument("--json", action="store_true")
    cp = ct.add_mutually_exclusive_group()
    cp.add_argument(
        "--usd-per-hour", type=float, help="compute: host, task or pod price per hour of run time"
    )
    cp.add_argument(
        "--lambda-memory-mb", type=float, help="compute: Lambda memory; priced per GB-second plus request"
    )
    cp.add_argument(
        "--bill-usd", type=float, help="compute: a shared host's bill, split over runs by wall time"
    )
    ct.add_argument(
        "--usd-per-gb-second", type=float, default=None, help="Lambda price (default us-east-1 x86)"
    )
    ct.add_argument("--usd-per-request", type=float, default=None, help="Lambda request fee")
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

    rp = cmd("repair", "propose a fix for a failed run as a diff (never applied)")
    rp.add_argument("run_id")
    rp.add_argument("--pipeline", required=True, help="the pipeline file the run used")
    rp.add_argument("--llm", help="llm name from the pipeline's llm: section (default: default)")
    rp.add_argument("--write", metavar="PATCHED.yaml", help="write the patched YAML to this new file")
    rp.add_argument("--json", action="store_true", help="print the result as JSON")

    rb = cmd("report", "a ready-to-paste bug report and prefilled issue URL (sends nothing unless --submit)")
    rb.add_argument("run_id", nargs="?", help="default: the latest failed run")
    rb.add_argument("--pipeline", help="the pipeline file the run used (secrets are masked)")
    rb.add_argument("--crash", action="store_true", help="report the last CLI crash instead of a run")
    rb.add_argument("-o", "--output", help="also write the markdown to this file")
    rb.add_argument("--json", action="store_true", help="print the report as JSON")
    rb.add_argument(
        "--submit",
        action="store_true",
        help="after printing, ask and then file it with your logged-in gh CLI (checks for duplicates first)",
    )

    st = cmd("stats", "success rate, durations, cost and failures by reason, per pipeline")
    st.add_argument("pipeline", nargs="?", help="pipeline name (default: all)")
    st.add_argument("--since", default="30d", help="how far back: 7d, 12h, 4w (default 30d)")
    st.add_argument("--json", action="store_true", help="print the report as JSON")
    st.add_argument(
        "--label",
        action="append",
        metavar="KEY=VALUE",
        help="only runs with this label, key=value (repeatable)",
    )

    sa = cmd("state", "cursors of incremental steps: show them, or reset to load from the start")
    sa.add_argument("action", choices=["show", "reset"])
    sa.add_argument("pipeline", nargs="?", help="pipeline name (show: default all; reset: required)")
    sa.add_argument("--step", help="reset only this step's cursor")

    ca = cmd("cache", "cached results in the run store: clear one key, a prefix (key*) or all")
    ca.add_argument("action", choices=["clear"])
    ca.add_argument("key", nargs="?", help="the key, or a prefix ending in * (e.g. 'catalog:*')")
    ca.add_argument("--all", action="store_true", help="clear every cached result, agent answers included")

    se = cmd("seen", "keys skip_seen already let through: list them, or forget them to reprocess")
    se.add_argument("action", choices=["list", "forget"])
    se.add_argument("key", nargs="?", help="forget: only this key (default: the whole scope)")
    se.add_argument("--scope", help="only this scope (default scope: the pipeline name)")

    mp = cmd("mappings", "column mappings of map_schema steps: show, approve or forget them")
    mp.add_argument("action", choices=["show", "approve", "forget"])
    mp.add_argument("pipeline", nargs="?", help="pipeline name (show: default all; else required)")
    mp.add_argument("step", nargs="?", help="the map_schema step (approve / forget: required)")
    mp.add_argument("--layout", help="approve / forget only this layout signature")

    rs = cmd("resolve", "model decisions of resolve steps: export them for review, import them back")
    rs.add_argument("action", choices=["export", "import"])
    rs.add_argument("pipeline", help="pipeline name")
    rs.add_argument("step", help="the resolve step id")
    rs.add_argument("file", nargs="?", help="JSON file (export: default stdout; import: required)")

    rv = cmd("review", "answers waiting for a person: list, show, accept, fix, reject, export")
    rv.add_argument("action", choices=["list", "show", "accept", "fix", "reject", "export"])
    rv.add_argument("args", nargs="*", help="ids; fix: one id, then field=value pairs")
    rv.add_argument("--pipeline", help="list / export: only this pipeline (by name)")
    rv.add_argument("--step", help="list / export: only this step")
    rv.add_argument(
        "--status",
        choices=["pending", "accepted", "fixed", "rejected", "all"],
        default="pending",
        help="list: which answers (default pending)",
    )
    rv.add_argument("--value", help="fix: the corrected fields as a JSON object")
    rv.add_argument("--golden", help="export: the golden JSONL file to append to")
    rv.add_argument("-n", type=int, default=50, help="list: at most this many (default 50)")

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
    _param_arg(sv)
    sv.add_argument("--host", default="127.0.0.1", help="webhook address (default 127.0.0.1)")
    sv.add_argument("--port", type=int, default=7071)

    sc = cmd("schedule", "show when a pipeline's schedule triggers fire next")
    sc.add_argument("action", choices=["next"])
    sc.add_argument("pipeline")
    sc.add_argument("--n", type=int, default=10, help="how many firings (default 10)")

    mc = cmd("mcp", "serve agentl as an MCP server (stdio or HTTP), or install it in an MCP client")
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
    mc.add_argument(
        "--http",
        metavar="[HOST]:PORT",
        help="serve streamable HTTP at /mcp instead of stdio, e.g. :7072 (default host 127.0.0.1)",
    )
    mc.add_argument(
        "--token-env",
        default="AGENTL_MCP_TOKEN",
        help="--http: env var holding the bearer token clients must send (default AGENTL_MCP_TOKEN)",
    )

    sn = cmd("snapshot", "run a pipeline and compare step outputs with saved snapshots, by key")
    sn.add_argument("pipeline")
    sn.add_argument("--dir", default="snapshots", help="snapshot folder (default: snapshots/<pipeline name>)")
    sn.add_argument("--step", action="append", help="only this step (repeatable; default: every step)")
    sn.add_argument("--key", help="fields that identify a record, comma separated (e.g. id or id,date)")
    sn.add_argument(
        "--update-snapshots", "-u", action="store_true", help="show the diff and ask to accept it"
    )
    sn.add_argument("--yes", action="store_true", help="with -u: accept without asking")
    sn.add_argument("--no-cache", action="store_true")
    _param_arg(sn)

    dg = cmd("digest", "send the notify.digest of check findings when due (cron), once per period")
    dg.add_argument("pipeline")
    dg.add_argument("--force", action="store_true", help="send now, even if not due or already sent")
    dg.add_argument("--show", action="store_true", help="print the digest, send nothing")
    dg.add_argument(
        "--errors", action="store_true", help="only the run errors section, with its own sent-today guard"
    )
    _param_arg(dg)

    gp = cmd("gaps", "missing partitions (days, hours, key x day) in the destination, from expect:")
    gp.add_argument("pipeline")
    gp.add_argument("--since", help="first period to check (default: expect.partitions.from)")
    gp.add_argument("--until", help="last period to check (default: the last complete one)")
    gp.add_argument("--json", action="store_true", help="print the report as JSON")
    gp.add_argument("--commands", action="store_true", help="print `agentl backfill` lines for the gaps")
    gp.add_argument("--fail", action="store_true", help="exit 1 when something is missing (cron, CI)")
    gp.add_argument("--fill", action="store_true", help="backfill only the missing partitions")
    gp.add_argument("--max", type=int, help="with --fill: at most this many runs (oldest first)")
    gp.add_argument(
        "--param-name", help="with --fill: the param that gets each period (default: the only one)"
    )
    gp.add_argument("--concurrency", type=int, default=1, help="with --fill: runs at a time")
    gp.add_argument(
        "--notify", action="append", metavar="CHANNEL", help="send the gaps to a notify: channel (repeatable)"
    )
    _param_arg(gp)

    bf = cmd(
        "backfill",
        "one run per file or per date (params) or per window chunk (`window:`), resumable",
    )
    bf.add_argument("pipeline")
    src = bf.add_mutually_exclusive_group(required=True)
    src.add_argument("--glob", help="one run per matching file (local or s3://, gs://... with [remote])")
    src.add_argument("--from", dest="start", metavar="DATE", help="first date (ISO), with --to")
    bf.add_argument("--to", dest="end", metavar="DATE", help="last date, included")
    bf.add_argument("--every", choices=["day", "week", "month"], default="day")
    bf.add_argument("--param-name", help="the param that gets each file or date (default: the only one)")
    bf.add_argument("--concurrency", type=int, default=1)
    bf.add_argument(
        "--redo",
        "--no-resume",
        dest="redo",
        action="store_true",
        help="run items (or window chunks) that already succeeded again",
    )
    bf.add_argument("--no-cache", action="store_true")
    bf.add_argument(
        "--log-format", choices=["text", "json"], default=os.environ.get("AGENTL_LOG_FORMAT", "text")
    )
    bf.add_argument("--otel", action="store_true", help=argparse.SUPPRESS)
    bf.add_argument("--sentry", action="store_true", help=argparse.SUPPRESS)
    bf.add_argument("--json", action="store_true", help="print the final report as JSON")
    _param_arg(bf)

    ds = cmd("dashboard", "open the local dashboard, or export it as static files for a bucket")
    ds.add_argument("action", nargs="?", choices=["serve", "export"], default="serve")
    ds.add_argument("out_dir", nargs="?", help="export: folder to write index.html and JSON into")
    ds.add_argument("--since", default="30d", help="export: how far back (default 30d)")
    ds.add_argument(
        "--label",
        action="append",
        metavar="KEY=VALUE",
        help="only runs with this label, key=value (repeatable)",
    )
    ds.add_argument("--max-runs", type=int, default=200, help="export: runs with full detail")
    ds.add_argument("--port", type=int, default=7070)
    ds.add_argument("--host", default="127.0.0.1")

    a = p.parse_args(argv)
    try:
        if a.cmd == "run":
            if not a.no_check and not _checked(a.pipeline):
                return 2
            spec = load(a.pipeline, params=_params(a.param))
            if spec.window is not None:
                return _windowed(a, spec)
            with open_store(a.db) as store:
                runner = Runner(spec, store=store, use_cache=not a.no_cache, listeners=_listeners(a))
                result = runner.run()
                _bug_hint(store, spec, result)
            if a.show:
                print(json.dumps(result.outputs.get(a.show, []), ensure_ascii=False, indent=2, default=str))
            return _exit_code(spec, result)
        if a.cmd == "resume":
            from .resume import ResumeRefused

            spec = load(a.pipeline, params=_params(a.param))
            with open_store(a.db) as store:
                runner = Runner(
                    spec,
                    store=store,
                    use_cache=not a.no_cache,
                    listeners=_listeners(a),
                    resume_from=a.run_id,
                )
                try:
                    result = runner.run()
                except ResumeRefused as e:
                    print(f"cannot resume: {e}", file=sys.stderr)
                    return 2
            return _exit_code(spec, result)
        if a.cmd == "snapshot":
            return _snapshot(a)
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
        if a.cmd == "gaps":
            return _gaps(a)
        if a.cmd == "digest":
            return _digest(a)
        if a.cmd == "diff":
            from . import diffing

            diffed = diffing.run_diff(load(a.pipeline), _fixtures(a.fixture), store=a.db, echo=a.echo)
            print(json.dumps(diffed.to_dict(), indent=2, default=str) if a.json else diffed.text(a.limit))
            if diffed.status != "succeeded":
                return 1
            return 1 if a.exit_code and diffed.changed else 0
        if a.cmd == "test":
            from . import testing

            fixtures = _fixtures(a.fixture)
            tested = testing.run_test(
                load(a.pipeline),
                fixtures,
                a.expect,
                key=a.key,
                ignore=testing.IGNORED + tuple(a.ignore),
                update=a.update,
            )
            if a.update:
                print(f"wrote {a.expect} ({tested.status})")
                return 0 if tested.status == "succeeded" else 1
            print(tested.text(), file=sys.stdout if tested.ok else sys.stderr)
            return 0 if tested.ok else 1
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
        if a.cmd == "eval":
            from . import golden

            if a.pipeline == "add":
                if not (a.step and a.add_step and a.from_run):
                    print("usage: agentl eval add PIPELINE STEP --from-run RUN_ID", file=sys.stderr)
                    return 2
                spec = load(a.step)
                step = spec.step(a.add_step)
                target = golden.golden_path(spec, step)
                with open_store(a.db) as store:
                    lines, origin = golden.run_records(store, a.from_run, step)
                added = golden.append_lines(target, lines, list(step.output or {}))
                print(
                    f"added {added} golden record(s) to {target} from run {a.from_run} ({origin}); "
                    "check and correct the answers by hand"
                )
                return 0
            if a.add_step or a.from_run:
                print("--from-run goes with `agentl eval add PIPELINE STEP`", file=sys.stderr)
                return 2
            spec = load(a.pipeline)
            variants = [golden.Variant("default")] + [golden.parse_compare(c) for c in a.compare]
            reports = [
                golden.compare(spec, s, v, a.pipeline)
                for s in golden.golden_steps(spec, a.step)
                for v in variants
            ]
            if a.json:
                print(json.dumps([r.as_dict() for r in reports], ensure_ascii=False, indent=2, default=str))
            else:
                print(golden.render(reports))
            return 0 if all(r.passed for r in reports) else 1
        if a.cmd == "diagram":
            print(graph.mermaid(load(a.pipeline)))
            return 0
        if a.cmd == "runs" and a.action == "show":
            from . import ledger

            if not a.run_id:
                print("agentl runs show needs a run id", file=sys.stderr)
                return 2
            with open_store(a.db) as store:
                report = ledger.show(store, a.run_id)
            if report is None:
                print(f"error: no run {a.run_id} in this store", file=sys.stderr)
                return 2
            print(json.dumps(report, indent=2, default=str) if a.json else ledger.show_text(report))
            return 0
        if a.cmd == "cost":
            from . import ledger
            from .checks import parse_duration

            by = [k.strip() for k in a.by.split(",") if k.strip()]
            lam = {
                k: v
                for k, v in (
                    ("usd_per_gb_second", a.usd_per_gb_second),
                    ("usd_per_request", a.usd_per_request),
                )
                if v is not None
            }
            if lam and a.lambda_memory_mb is None:
                print("--usd-per-gb-second and --usd-per-request need --lambda-memory-mb", file=sys.stderr)
                return 2
            price = ledger.compute_price(a.usd_per_hour, a.lambda_memory_mb, a.bill_usd, **lam)
            with open_store(a.db) as store:
                report = ledger.cost(store, time.time() - parse_duration(a.since), by, compute=price)
            print(json.dumps(report, indent=2, default=str) if a.json else ledger.cost_text(report))
            return 0
        if a.cmd == "runs":
            with open_store(a.db) as store:
                runs = store.runs(labels=parse_filters(a.label))
            for run in runs:
                outcome = run.get("outcome") or "-"
                status = f"{run['status']:<10} {outcome:<9}"
                tags = " ".join(f"{k}={v}" for k, v in sorted(run["labels"].items()))
                tags = f"  [{tags}]" if tags else ""
                print(f"{run['id']}  {status} {run['pipeline']}{tags}  {run['error'] or ''}")
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
        if a.cmd == "repair":
            from . import repair

            if a.write and Path(a.write).resolve() == Path(a.pipeline).resolve():
                print("error: --write must name a new file, not the pipeline itself", file=sys.stderr)
                return 2
            text = Path(a.pipeline).read_text(encoding="utf-8")
            with open_store(a.db) as store:
                proposal = repair.propose(store, a.run_id, text, a.pipeline, a.llm)
            if a.write and proposal["valid"]:
                repair.write_patched(proposal, a.write, a.pipeline)
                proposal["written"] = a.write
            if a.json:
                print(json.dumps(proposal, indent=2, default=str))
            else:
                print(repair.text(proposal, color=sys.stdout.isatty()))
                if proposal.get("written"):
                    print(f"wrote {a.write} (the pipeline file is unchanged)")
            return 0 if proposal["valid"] else 1
        if a.cmd == "report":
            return _report(a)
        if a.cmd == "stats":
            from . import stats
            from .checks import parse_duration

            with open_store(a.db) as store:
                report = stats.compute(
                    store,
                    time.time() - parse_duration(a.since),
                    a.pipeline,
                    labels=parse_filters(a.label),
                )
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
        if a.cmd == "cache":
            if bool(a.key) == bool(a.all):
                print("agentl cache clear needs a key or --all", file=sys.stderr)
                return 2
            with open_store(a.db) as store:
                count = store.cache_clear(None if a.all else a.key)
            print(f"cleared {count} cached result(s)")
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

        if a.cmd == "mappings":
            return _mappings(a)
        if a.cmd == "rejected":
            return _rejected(a)
        if a.cmd == "review":
            return _review(a)
        if a.cmd == "schedule":
            return _schedule_next(load(a.pipeline), a.n)

        if a.cmd == "resolve":
            return _resolve_decisions(a)
        if a.cmd == "serve":
            import logging

            from .triggers import Service

            serve_log = logging.getLogger("agentl.serve")
            if not serve_log.handlers:  # one line per run on the terminal, unless logging is set up
                handler = logging.StreamHandler()
                handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
                serve_log.addHandler(handler)
                serve_log.setLevel(logging.INFO)
            fixed = _params(a.param)
            specs = [
                load(p, params={k: v for k, v in fixed.items() if k in _declared(p)}) for p in a.pipelines
            ]
            for spec in specs:
                assist.check(spec)
            with open_store(a.db) as store:
                Service(specs, store).serve(a.host, a.port)
            return 0
        if a.cmd == "backfill":
            from . import backfill as bfill

            if a.start and not a.end:
                raise ValueError("--from needs --to")
            # One command, two modes: a pipeline with `window:` is backfilled chunk by chunk
            # over [--from, --to] (both inclusive); otherwise one run per file or date, each
            # passed as a run parameter.
            if _has_window(a.pipeline):
                return _window_backfill(a)
            items = bfill.periods(a.start, a.end, a.every) if a.start else bfill.files(a.glob)
            if not items:
                print("backfill: nothing matches", file=sys.stderr)
                return 0

            def progress(item: str, status: str, err: str | None) -> None:
                print(f"{status} {item}" + (f": {err}" if err else ""), file=sys.stderr)

            outcome = bfill.backfill(
                a.pipeline,
                items,
                param=a.param_name,
                params=_params(a.param),
                db=a.db,
                concurrency=a.concurrency,
                redo=a.redo,
                kind="date" if a.start else "str",
                on_done=progress,
            )
            print(json.dumps(outcome.to_dict(), indent=2) if a.json else outcome.text())
            return 0 if outcome.ok else 1
        if a.cmd == "mcp":
            if a.action == "serve":
                from .mcp_server import serve as serve_mcp

                serve_mcp(a.db, a.root, a.allow_code, http=a.http, token_env=a.token_env)
                return 0
            return _mcp_install(a)
        if a.cmd == "dashboard" and a.action == "export":
            from .checks import parse_duration
            from .dashboard import export

            if not a.out_dir:
                print("agentl dashboard export needs an output folder", file=sys.stderr)
                return 2
            with open_store(a.db) as store:
                done = export(
                    store,
                    a.out_dir,
                    time.time() - parse_duration(a.since),
                    parse_filters(a.label),
                    max_runs=a.max_runs,
                )
            print(f"wrote {done['dir']}/index.html: {done['runs']} runs, {done['details']} with detail")
            return 0
        if a.cmd == "dashboard":
            from .dashboard import serve

            with open_store(a.db) as store:
                serve(store, a.host, a.port)
            return 0
    except (SpecError, KeyError, FileNotFoundError, FileExistsError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception:
        if a.cmd == "report":
            raise
        import traceback

        from . import report as bug_report

        trace = traceback.format_exc()
        print(trace, end="", file=sys.stderr)
        bug_report.save_crash(list(argv if argv is not None else sys.argv[1:]), trace)
        line = bug_report.hint()
        if line:
            print(line, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
