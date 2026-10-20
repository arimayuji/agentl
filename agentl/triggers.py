"""`agentl serve`: run pipelines on schedules, webhooks and new files.

    trigger:
      - cron: "0 7 * * 1-5"                       # weekdays at 07:00 (local time, or `timezone`)
      - webhook: /hooks/invoices                  # POST JSON here; `secret` to require auth
        secret: ${INVOICES_HOOK_SECRET}
      - files: "inbox/*.csv"                      # new or changed files, local or remote glob
        every: 1m
      - after: extract_orders                     # when that pipeline finishes (`status`)

A `use: trigger` step gets what started the run: the webhook body as records, one record per new
file (`path`, `name`, `size`, `modified`), `{scheduled_at}` for a schedule, or the finished
parent run (`pipeline`, `run_id`, `status`, `error`) for `after`. `on_success` / `on_failure` /
`after` actions chain pipelines and emit events (see agentl.chain). Runs of the same
pipeline never overlap: they queue one after the other. Standard library only."""

from __future__ import annotations

import datetime as _dt
import glob
import hashlib
import hmac
import json
import logging
import os
import queue
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger("agentl.serve")

# ---------------------------------------------------------------- cron
_NAMES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}
_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
_MACROS = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *"}


class Cron:
    """Five-field cron: minute hour day-of-month month day-of-week, with `*`, lists, ranges, steps
    and names. As in classic cron, when both day fields are restricted either one matching is enough."""

    def __init__(self, expr: str):
        self.expr = expr
        fields = _MACROS.get(expr.strip(), expr).split()
        if len(fields) != 5:
            raise ValueError(f"cron '{expr}': use five fields: minute hour day month weekday")
        self.sets = [self._field(f, lo, hi, expr) for f, (lo, hi) in zip(fields, _RANGES, strict=True)]
        self.sets[4] = {d % 7 for d in self.sets[4]}  # 7 is Sunday too
        self.dom_any, self.dow_any = fields[2] == "*", fields[4] == "*"

    @staticmethod
    def _field(text: str, lo: int, hi: int, expr: str) -> set[int]:
        out: set[int] = set()
        for part in text.lower().split(","):
            rng, _, step = part.partition("/")
            try:
                every = int(step) if step else 1
                if rng == "*":
                    a, b = lo, hi
                elif "-" in rng:
                    x, y = rng.split("-", 1)
                    a, b = int(_NAMES.get(x, x)), int(_NAMES.get(y, y))
                else:
                    a = int(_NAMES.get(rng, rng))
                    b = hi if step else a
            except ValueError as e:
                raise ValueError(f"cron '{expr}': cannot read '{part}'") from e
            if not (lo <= a <= hi and lo <= b <= hi) or every < 1:
                raise ValueError(f"cron '{expr}': '{part}' is out of range {lo}-{hi}")
            out.update(range(a, b + 1, every))
        return out

    def matches(self, t: _dt.datetime) -> bool:
        minute, hour, dom, month, dow = self.sets
        if t.minute not in minute or t.hour not in hour or t.month not in month:
            return False
        day_ok, week_ok = t.day in dom, (t.isoweekday() % 7) in dow
        if self.dom_any or self.dow_any:
            return day_ok and week_ok
        return day_ok or week_ok

    def next_after(self, t: _dt.datetime) -> _dt.datetime:
        t = t.replace(second=0, microsecond=0) + _dt.timedelta(minutes=1)
        for _ in range(60 * 24 * 366 * 5):
            if self.matches(t):
                return t
            t += _dt.timedelta(minutes=1)
        raise ValueError(f"cron '{self.expr}' never fires")


def _now(tz: str | None) -> _dt.datetime:
    if tz:
        from zoneinfo import ZoneInfo

        return _dt.datetime.now(ZoneInfo(tz))
    return _dt.datetime.now()


# ---------------------------------------------------------------- files
def list_files(pattern: str, storage_options: dict | None = None) -> list[dict]:
    """Files matching a local or remote (fsspec) glob, with size and modification time."""
    if "://" in pattern and not pattern.startswith("file://"):
        from .registry import require

        fsspec = require("fsspec", "remote")
        fs, _, paths = fsspec.get_fs_token_paths(pattern, storage_options=storage_options or {})
        proto = pattern.split("://", 1)[0]
        out = []
        for p in fs.glob(paths[0]):
            info = fs.info(p)
            if info.get("type") == "directory":
                continue
            mod = info.get("mtime") or info.get("LastModified") or info.get("updated") or info.get("created")
            out.append(
                {
                    "path": f"{proto}://{p}",
                    "name": p.rsplit("/", 1)[-1],
                    "size": info.get("size"),
                    "modified": str(mod),
                }
            )
        return out
    out = []
    for p in sorted(glob.glob(pattern.removeprefix("file://"), recursive=True)):
        if os.path.isfile(p):
            st = os.stat(p)
            out.append(
                {
                    "path": p,
                    "name": os.path.basename(p),
                    "size": st.st_size,
                    "modified": _dt.datetime.fromtimestamp(st.st_mtime).isoformat(),
                }
            )
    return out


def new_files(store: Any, pipeline: str, pattern: str, storage_options: dict | None = None) -> list[dict]:
    """Files that are new or changed since they were last handed to a run. The cursor (path ->
    size and mtime) lives in the run store, so a restart does not reprocess old files."""
    key = "files-cursor:" + hashlib.sha256(f"{pipeline}\0{pattern}".encode()).hexdigest()
    seen: dict = store.cache_get(key) or {}
    fresh = [
        f for f in list_files(pattern, storage_options) if seen.get(f["path"]) != [f["size"], f["modified"]]
    ]
    return fresh


def mark_seen(store: Any, pipeline: str, pattern: str, files: list[dict]) -> None:
    key = "files-cursor:" + hashlib.sha256(f"{pipeline}\0{pattern}".encode()).hexdigest()
    seen: dict = store.cache_get(key) or {}
    seen.update({f["path"]: [f["size"], f["modified"]] for f in files})
    store.cache_set(key, seen)


# ---------------------------------------------------------------- webhooks
def authorized(secret: str | None, headers: Any, body: bytes) -> bool:
    """No secret: open. Else `Authorization: Bearer <secret>`, or `X-Signature-256: sha256=<hex>`
    with the HMAC-SHA256 of the body (also accepted as GitHub's `X-Hub-Signature-256`)."""
    if not secret:
        return True
    auth = headers.get("Authorization") or ""
    if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].encode(), secret.encode()):
        return True
    sig = headers.get("X-Signature-256") or headers.get("X-Hub-Signature-256") or ""
    want = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(sig) and hmac.compare_digest(sig.encode(), want.encode())


def body_records(body: bytes) -> list[dict]:
    if not body.strip():
        return []
    data = json.loads(body)
    if isinstance(data, list):
        return [d if isinstance(d, dict) else {"value": d} for d in data]
    return [data if isinstance(data, dict) else {"value": data}]


# ---------------------------------------------------------------- service
class Service:
    """Owns the pipelines, one worker queue per pipeline (runs never overlap), the schedule and
    file checks (`tick`), and the webhook routes."""

    def __init__(
        self, specs: list[Any], store: Any, run: Callable[[Any, list[dict], str], Any] | None = None
    ):
        self.specs = {s.name: s for s in specs}
        self.store = store
        self._run = run or self._default_run
        self.queues: dict[str, queue.Queue] = {}
        self.routes: dict[str, tuple[Any, Any]] = {}
        self.next_cron: dict[tuple[str, int], _dt.datetime] = {}
        self.next_files: dict[tuple[str, int], float] = {}
        self.next_digest: dict[str, float] = {}  # pipeline -> when to look at notify.digest again
        self.schedules: dict[tuple[str, int], Any] = {}
        self.next_cycle: dict[tuple[str, int], tuple[_dt.date, _dt.datetime] | None] = {}
        self.open_cycle: dict[tuple[str, int], _dt.date] = {}  # last cycle fired, until it closes
        self.on_late: Callable[[Any, _dt.date, _dt.datetime], None] | None = None
        self.pending: dict[tuple[str, int], set[str]] = {}  # files handed to a run not finished yet
        self._stop = threading.Event()
        from . import chain

        chain.check(specs)
        chained = {a.trigger for s in specs for a in [*s.on_success, *s.on_failure, *s.after]}
        for spec in specs:
            if not spec.trigger and spec.name not in chained:
                raise ValueError(f"pipeline '{spec.name}' has no trigger; add one under `trigger:`")
            for i, t in enumerate(spec.trigger):
                if t.webhook:
                    if t.webhook in self.routes:
                        raise ValueError(f"two pipelines use webhook {t.webhook}")
                    self.routes[t.webhook] = (spec, t)
                if t.cron:
                    self.next_cron[(spec.name, i)] = Cron(t.cron).next_after(_now(t.timezone))
                if t.schedule:
                    from .schedule import Schedule

                    sch = self.schedules[(spec.name, i)] = Schedule(t.schedule)
                    nxt = sch.next_firings(_dt.datetime.now(_dt.timezone.utc), 1)
                    self.next_cycle[(spec.name, i)] = nxt[0] if nxt else None

    def _default_run(self, spec: Any, records: list[dict], kind: str) -> Any:
        from .runner import Runner

        return Runner(spec, store=self.store, trigger=records).run()

    def submit(
        self,
        spec: Any,
        records: list[dict],
        kind: str,
        after: Callable[[bool], None] | None = None,
        parent: dict | None = None,
        chain: tuple[str, ...] = (),
    ) -> None:
        """Queue a run; runs of one pipeline go one at a time in its own worker thread. `parent`
        and `chain` are set when another pipeline's run started this one."""
        q = self.queues.get(spec.name)
        if q is None:
            q = self.queues[spec.name] = queue.Queue()
            threading.Thread(target=self._worker, args=(q,), daemon=True, name=f"agentl-{spec.name}").start()
        q.put((spec, records, kind, after, parent, chain))

    def _worker(self, q: queue.Queue) -> None:
        while True:
            spec, records, kind, after, parent, chain = q.get()
            ok = False
            result = None
            try:
                result = self._run(spec, records, kind)
                ok = getattr(result, "status", "succeeded") == "succeeded"
                error = getattr(result, "error", None)
                log.info(
                    "%s: %s run %s %s%s",
                    spec.name,
                    kind,
                    getattr(result, "run_id", ""),
                    getattr(result, "status", "?"),
                    f" ({error})" if error else "",
                )
            except Exception:  # a broken run must not stop the service
                log.exception("%s: %s run crashed", spec.name, kind)
            finally:
                if after:
                    after(ok)
                try:
                    self._link(spec, result, parent)
                    self._then(spec, records, result, (*chain, spec.name))
                except Exception:
                    log.exception("%s: after-run actions failed", spec.name)
                q.task_done()

    def _link(self, spec: Any, result: Any, parent: dict | None) -> None:
        """Record parent <-> child on both runs, so the dashboard shows where a run came from."""
        run_id = getattr(result, "run_id", None)
        if not parent or not run_id or not hasattr(self.store, "event"):
            return
        status = getattr(result, "status", "failed")
        self.store.event(run_id, "chain.parent", None, parent)
        self.store.event(
            parent["run_id"], "chain.child", None, {"pipeline": spec.name, "run_id": run_id, "status": status}
        )

    def _then(self, spec: Any, records: list[dict], result: Any, chain: tuple[str, ...]) -> None:
        """Run the `on_success` / `on_failure` / `after` actions and start `after:` triggers."""
        from . import chain as ch

        m = ch.metrics(result)
        ctx = ch.context(spec, result, records)
        parent = {"pipeline": spec.name, "run_id": ctx["run"]["id"], "status": m["status"]}
        actions = [*(spec.on_success if m["status"] == "succeeded" else spec.on_failure), *spec.after]
        for a in actions:
            if a.when and not ch.evaluate(a.when, m):
                continue
            if a.emit:
                try:
                    ch.emit(a.emit, ctx)
                except Exception as e:  # an unreachable receiver must not stop the service
                    log.warning("%s: emit %s failed: %s", spec.name, a.emit.topic, e)
            elif a.trigger:
                self._start(a.trigger, [ch.resolve(a.with_, ctx)], parent, chain)
        for name, other in self.specs.items():
            for t in other.trigger:
                if t.after == spec.name and t.status in (m["status"], "any"):
                    rec = {**parent, "error": ctx["run"]["error"]}
                    self._start(name, [rec], parent, chain)

    def _start(self, name: str, records: list[dict], parent: dict, chain: tuple[str, ...]) -> None:
        if name in chain:  # checked when the service starts; this is the runtime backstop
            log.error("%s: not started, it would chain in a cycle: %s", name, " -> ".join([*chain, name]))
            return
        self.submit(self.specs[name], records, "chain", parent=parent, chain=chain)

    def tick(self, now: float | None = None) -> None:
        """Start the runs that are due: schedules whose time came, and file triggers with new files.
        Files are marked as seen only after a successful run, so a failed run sees them again."""
        wall = time.time() if now is None else now
        from .checks import parse_duration

        for name, spec in self.specs.items():
            for i, t in enumerate(spec.trigger):
                if t.cron:
                    due = self.next_cron[(name, i)]
                    current = _dt.datetime.fromtimestamp(wall, due.tzinfo)
                    if current >= due:
                        self.next_cron[(name, i)] = Cron(t.cron).next_after(current)
                        self.submit(spec, [{"scheduled_at": due.isoformat()}], "cron")
                elif t.schedule:
                    self._tick_schedule(spec, i, wall)
                elif t.kind == "every":
                    q = self.queues.get(name)
                    running = q is not None and q.unfinished_tasks > 0
                    if running or wall < self.next_files.get((name, i), 0):
                        continue  # back to back, never piling up
                    self.next_files[(name, i)] = wall + parse_duration(t.every)
                    self.submit(spec, [{"started_at": _dt.datetime.fromtimestamp(wall).isoformat()}], "every")
                elif t.files:
                    if wall < self.next_files.get((name, i), 0):
                        continue
                    self.next_files[(name, i)] = wall + parse_duration(t.every)
                    busy = self.pending.setdefault((name, i), set())
                    files = [
                        f
                        for f in new_files(self.store, name, t.files, t.storage_options)
                        if f["path"] not in busy
                    ]
                    if files:
                        busy.update(f["path"] for f in files)
                        self.submit(spec, files, "files", after=self._seen(name, i, t.files, files))
            self._tick_digest(spec, wall)

    def _tick_digest(self, spec: Any, wall: float) -> None:
        """Send the pipeline's `notify.digest` when it is due (checked once a minute)."""
        if not (spec.notify and spec.notify.digest) or wall < self.next_digest.get(spec.name, 0):
            return
        self.next_digest[spec.name] = wall + 60
        from .digest import send_due

        try:
            for channel, outcome in send_due(
                spec, self.store, _dt.datetime.fromtimestamp(wall, _dt.timezone.utc)
            ).items():
                if outcome == "sent" or outcome.startswith("failed"):
                    log.info("%s: digest to %s: %s", spec.name, channel, outcome)
        except Exception as e:  # the digest never stops the service
            log.warning("%s: digest failed: %s", spec.name, e)

    def _cycle_key(self, name: str, i: int, d: _dt.date) -> str:
        return f"schedule-cycle:{name}:{i}:{d.isoformat()}"

    def cycle_succeeded(self, name: str, i: int, d: _dt.date) -> bool:
        return bool(self.store.cache_get(self._cycle_key(name, i, d)))

    def _tick_schedule(self, spec: Any, i: int, wall: float) -> None:
        """Fire the schedule's due poll, skip polls of a cycle that already succeeded
        (`stop_when: success_in_cycle`) and raise a lateness alert when a window closes empty."""
        key, sch = (spec.name, i), self.schedules[(spec.name, i)]
        current = _dt.datetime.fromtimestamp(wall, _dt.timezone.utc)
        opened = self.open_cycle.get(key)
        q = self.queues.get(spec.name)
        busy = q is not None and q.unfinished_tasks > 0
        if opened is not None and current > sch.window_end(opened) and not busy:
            del self.open_cycle[key]
            if not self.cycle_succeeded(spec.name, i, opened):
                self._late(spec, opened, sch.start_of(opened))
        nxt = self.next_cycle.get(key)
        if nxt is None or current < nxt[1]:
            return
        d, due = nxt
        later = sch.next_firings(current, 1)
        self.next_cycle[key] = later[0] if later else None
        if opened is not None and opened != d and not self.cycle_succeeded(spec.name, i, opened):
            self.open_cycle.pop(key, None)
            self._late(spec, opened, sch.start_of(opened))
        self.open_cycle[key] = d
        stop = spec.trigger[i].schedule.poll and spec.trigger[i].schedule.poll.stop_when
        if stop and self.cycle_succeeded(spec.name, i, d):
            return
        if stop and busy:
            return  # a poll of this pipeline is still running; the next one decides

        def done(ok: bool) -> None:
            if ok:
                self.store.cache_set(self._cycle_key(spec.name, i, d), True)

        record = {
            "scheduled_at": due.isoformat(),
            "cycle": d.isoformat(),
            "expected_at": sch.start_of(d).isoformat(),
        }
        self.submit(spec, [record], "schedule", after=done)

    def _late(self, spec: Any, d: _dt.date, expected: _dt.datetime) -> None:
        log.warning(
            "%s: late: cycle %s (expected %s) had no successful run in its window",
            spec.name,
            d,
            expected.isoformat(),
        )
        if self.on_late:
            self.on_late(spec, d, expected)

    def _seen(self, name: str, i: int, pattern: str, files: list[dict]) -> Callable[[bool], None]:
        def done(ok: bool) -> None:
            if ok:
                mark_seen(self.store, name, pattern, files)
            self.pending[(name, i)].difference_update(f["path"] for f in files)

        return done

    def handler(self) -> type[BaseHTTPRequestHandler]:
        service = self

        class Hook(BaseHTTPRequestHandler):
            timeout = 30  # a client that stops sending is dropped instead of holding a thread

            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, code: int, data: dict) -> None:
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path == "/health":
                    return self._send(200, {"ok": True, "pipelines": sorted(service.specs)})
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                route = service.routes.get(self.path.split("?", 1)[0])
                if route is None:
                    return self._send(404, {"error": "no webhook at this path"})
                spec, t = route
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if n < 0:
                    return self._send(400, {"error": "bad Content-Length"})
                if n > 10 * 1024 * 1024:
                    return self._send(413, {"error": "body over 10 MB"})
                body = self.rfile.read(n) if n else b""
                if not authorized(t.secret, self.headers, body):
                    return self._send(401, {"error": "unauthorized"})
                try:
                    records = body_records(body)
                except json.JSONDecodeError:
                    return self._send(400, {"error": "body must be JSON"})
                service.submit(spec, records, "webhook")
                self._send(202, {"queued": spec.name, "records": len(records)})

        return Hook

    def serve(self, host: str, port: int, interval: float = 1.0) -> None:
        srv = None
        if self.routes:
            open_hooks = [p for p, (_, t) in self.routes.items() if not t.secret]
            if open_hooks and host not in ("127.0.0.1", "localhost", "::1"):
                print(
                    f"warning: webhooks without a secret are open to anyone who can reach {host}: "
                    f"{', '.join(sorted(open_hooks))}. Set `secret:` on them.",
                    file=sys.stderr,
                )
            srv = ThreadingHTTPServer((host, port), self.handler())
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            print(f"agentl serve: webhooks on http://{host}:{port} ({', '.join(sorted(self.routes))})")
        for name, spec in self.specs.items():
            kinds = ", ".join(
                t.cron
                or (f"schedule {t.schedule.tz}" if t.schedule else None)
                or t.webhook
                or (f"files {t.files}" if t.files else f"after {t.after}" if t.after else f"every {t.every}")
                for t in spec.trigger
            )
            print(f"  {name}: {kinds or 'chained'}")
        try:
            while not self._stop.wait(interval):
                self.tick()
        finally:
            if srv:
                srv.shutdown()

    def stop(self) -> None:
        self._stop.set()
