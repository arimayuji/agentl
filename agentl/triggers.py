"""`agentl serve`: run pipelines on schedules, webhooks and new files.

    trigger:
      - cron: "0 7 * * 1-5"                       # weekdays at 07:00 (local time, or `timezone`)
      - webhook: /hooks/invoices                  # POST JSON here; `secret` to require auth
        secret: ${INVOICES_HOOK_SECRET}
      - files: "inbox/*.csv"                      # new or changed files, local or remote glob
        every: 1m

A `use: trigger` step gets what started the run: the webhook body as records, one record per new
file (`path`, `name`, `size`, `modified`), or `{scheduled_at}` for a schedule. Runs of the same
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
        self.pending: dict[tuple[str, int], set[str]] = {}  # files handed to a run not finished yet
        self._stop = threading.Event()
        for spec in specs:
            if not spec.trigger:
                raise ValueError(f"pipeline '{spec.name}' has no trigger; add one under `trigger:`")
            for i, t in enumerate(spec.trigger):
                if t.webhook:
                    if t.webhook in self.routes:
                        raise ValueError(f"two pipelines use webhook {t.webhook}")
                    self.routes[t.webhook] = (spec, t)
                if t.cron:
                    self.next_cron[(spec.name, i)] = Cron(t.cron).next_after(_now(t.timezone))

    def _default_run(self, spec: Any, records: list[dict], kind: str) -> Any:
        from .runner import Runner

        return Runner(spec, store=self.store, trigger=records).run()

    def submit(
        self, spec: Any, records: list[dict], kind: str, after: Callable[[bool], None] | None = None
    ) -> None:
        """Queue a run; runs of one pipeline go one at a time in its own worker thread."""
        q = self.queues.get(spec.name)
        if q is None:
            q = self.queues[spec.name] = queue.Queue()
            threading.Thread(target=self._worker, args=(q,), daemon=True, name=f"agentl-{spec.name}").start()
        q.put((spec, records, kind, after))

    def _worker(self, q: queue.Queue) -> None:
        while True:
            spec, records, kind, after = q.get()
            ok = False
            try:
                result = self._run(spec, records, kind)
                ok = getattr(result, "status", "succeeded") == "succeeded"
                log.info("%s: %s run %s", spec.name, kind, getattr(result, "status", "?"))
            except Exception:  # a broken run must not stop the service
                log.exception("%s: %s run crashed", spec.name, kind)
            finally:
                if after:
                    after(ok)
                q.task_done()

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
                t.cron or t.webhook or (f"files {t.files}" if t.files else f"every {t.every}")
                for t in spec.trigger
            )
            print(f"  {name}: {kinds}")
        try:
            while not self._stop.wait(interval):
                self.tick()
        finally:
            if srv:
                srv.shutdown()

    def stop(self) -> None:
        self._stop.set()
