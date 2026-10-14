"""Benchmark scenarios. Each returns a dict with a human `summary` plus raw numbers."""

from __future__ import annotations

import csv
import http.server
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from agentl import parse, register_provider
from agentl.llm import Result
from agentl.registry import get
from agentl.runner import Runner
from agentl.store import Store


class Skip(Exception):
    pass


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="agentl-bench-"))


def _timed(fn):
    t0 = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t0


def _median_timed(fn, repeat: int = 3):
    """Run `fn` `repeat` times and return the last output with the median time (drops warm-up noise)."""
    times, out = [], None
    for _ in range(repeat):
        out, s = _timed(fn)
        times.append(s)
    return out, statistics.median(times)


def _rate(n: int, s: float) -> str:
    return f"{n / s:,.0f} records/s" if s else "n/a"


# ---------------------------------------------------------------- footprint
def import_time(scale: float) -> dict:
    """Median wall time of `python -c "import agentl"` in a fresh interpreter (5 runs)."""
    runs = []
    for _ in range(5):
        t0 = time.perf_counter()
        subprocess.run([sys.executable, "-c", "import agentl"], check=True)
        runs.append(time.perf_counter() - t0)
    base = []
    for _ in range(5):
        t0 = time.perf_counter()
        subprocess.run([sys.executable, "-c", "pass"], check=True)
        base.append(time.perf_counter() - t0)
    ms = (statistics.median(runs) - statistics.median(base)) * 1000
    optional = ["pandas", "numpy", "requests", "sqlalchemy", "boto3", "fsspec", "anthropic", "psycopg"]
    optional.append("playwright")
    probe = f"import agentl,sys;print(sorted(m for m in {optional!r} if m in sys.modules))"
    heavy = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()
    return {"summary": f"{ms:.0f} ms over bare python", "ms": round(ms, 1), "heavy_modules_imported": heavy}


# ---------------------------------------------------------------- files
def csv_read(scale: float) -> dict:
    n = int(200_000 * scale)
    d = _tmp()
    path = d / "sales.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "customer", "total", "date"])
        for i in range(n):
            w.writerow([i, f"c{i % 997}", f"{i * 1.37:.2f}", "2026-10-14"])
    rows, s = _median_timed(lambda: get("csv.read")(None, path=str(path)))
    assert len(rows) == n
    return {
        "summary": _rate(n, s),
        "records": n,
        "seconds": round(s, 3),
        "mb": round(path.stat().st_size / 1e6, 1),
    }


def transforms(scale: float) -> dict:
    n = int(200_000 * scale)
    rows = [{"id": i % (n // 2), "v": "x" if i % 3 else ""} for i in range(n)]

    def run():
        a = get("filter")(rows, not_empty="v")
        b = get("dedupe")(a, on="id")
        return get("select")(b, fields=["id"])

    out, s = _median_timed(run)
    return {"summary": _rate(n, s), "records_in": n, "records_out": len(out), "seconds": round(s, 3)}


def sqlite_write(scale: float) -> dict:
    n = int(100_000 * scale)
    rows = [{"id": i, "name": f"n{i}", "total": i * 0.5} for i in range(n)]
    d = _tmp()
    counter = iter(range(100))
    _, s = _median_timed(lambda: get("sqlite.write")(rows, db=str(d / f"b{next(counter)}.sqlite"), table="t"))
    return {"summary": _rate(n, s), "records": n, "seconds": round(s, 3)}


def postgres_upsert(scale: float) -> dict:
    dsn = os.environ.get("AGENTL_TEST_PG_DSN")
    if not dsn:
        raise Skip("set AGENTL_TEST_PG_DSN")
    n = int(100_000 * scale)
    rows = [{"id": i, "name": f"n{i}", "total": i * 0.5} for i in range(n)]
    table = f"bench_{random.randint(0, 1 << 30)}"
    w = get("postgres.write")
    try:
        _, s1 = _timed(lambda: w(rows, dsn=dsn, table=table, mode="upsert", key="id"))
        _, s2 = _timed(lambda: w(rows, dsn=dsn, table=table, mode="upsert", key="id"))
    finally:
        import psycopg

        with psycopg.connect(dsn) as conn:
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
    return {
        "summary": f"{_rate(n, s1)} (insert), {_rate(n, s2)} (update)",
        "records": n,
        "insert_s": round(s1, 3),
        "update_s": round(s2, 3),
    }


# ---------------------------------------------------------------- HTTP
class _Api(http.server.BaseHTTPRequestHandler):
    latency = 0.01

    def log_message(self, *a):
        pass

    def do_GET(self):
        time.sleep(self.latency)
        body = json.dumps({"items": [{"path": self.path}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def http_fanout(scale: float) -> dict:
    os.environ.setdefault("AGENTL_ALLOW_HOSTS", "127.0.0.1")
    n = int(1000 * scale)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Api)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/item/{{i}}"
    recs = [{"i": i} for i in range(n)]
    try:
        out = {}
        for c in (1, 16):
            _, s = _timed(lambda c=c: get("http.get")(recs, url=url, items="items", concurrency=c))
            out[f"c{c}_s"] = round(s, 3)
    finally:
        srv.shutdown()
        srv.server_close()
    speedup = out["c1_s"] / out["c16_s"]
    return {
        "summary": f"{n / out['c16_s']:,.0f} req/s at concurrency 16 ({speedup:.1f}x)",
        "requests": n,
        "server_latency_ms": 10,
        **out,
    }


# ---------------------------------------------------------------- agent steps
class _FakeLLM:
    """Fixed latency, fixed token usage: measures agentl's overhead and concurrency, not a model."""

    latency = 0.02

    def __init__(self, spec):
        pass

    def structured(self, instruction, record, schema):
        time.sleep(self.latency)
        return Result(schema.model_validate({"category": "software", "ok": True}), 400, 40)

    def text(self, instruction, content):
        return ""


register_provider("bench-fake", _FakeLLM)


def _agent_spec(concurrency: int):
    return parse(
        {
            "name": "bench",
            "llm": {"provider": "bench-fake", "model": "m", "price": {"input": 3.0, "output": 15.0}},
            "steps": [
                {"id": "src", "fn": "benchmarks.scenarios:_rows"},
                {
                    "id": "classify",
                    "agent": "Classify.",
                    "input": "src",
                    "concurrency": concurrency,
                    "output": {"category": "str", "ok": "bool"},
                },
            ],
        }
    )


_N_AGENT = 500


def _rows(input=None):
    return [{"i": i, "description": f"invoice line {i}"} for i in range(_N_AGENT)]


def agent_step(scale: float) -> dict:
    global _N_AGENT
    _N_AGENT = int(500 * scale)
    d = _tmp()
    out = {}
    for c in (1, 8, 32):
        with Store(d / f"c{c}.db") as store:
            r, s = _timed(lambda c=c, store=store: Runner(_agent_spec(c), store=store, use_cache=False).run())
        assert r.status == "succeeded", r.error
        out[f"c{c}_s"] = round(s, 2)
    overhead_ms = (out["c1_s"] - _N_AGENT * _FakeLLM.latency) / _N_AGENT * 1000
    return {
        "summary": f"{_N_AGENT / out['c32_s']:,.0f} records/s at concurrency 32",
        "records": _N_AGENT,
        "llm_latency_ms": int(_FakeLLM.latency * 1000),
        "overhead_ms_per_record": round(overhead_ms, 2),
        **out,
    }


def agent_cache(scale: float) -> dict:
    global _N_AGENT
    _N_AGENT = int(500 * scale)
    db = _tmp() / "cache.db"
    with Store(db) as store:
        r1, s1 = _timed(lambda: Runner(_agent_spec(8), store=store).run())
        r2, s2 = _timed(lambda: Runner(_agent_spec(8), store=store).run())
    return {
        "summary": f"re-run {s1 / s2:.0f}x faster, {r2.input_tokens} tokens",
        "records": _N_AGENT,
        "first_s": round(s1, 2),
        "rerun_s": round(s2, 3),
        "first_cost_usd": round(r1.cost_usd, 4),
        "rerun_cost_usd": round(r2.cost_usd, 4),
    }


ALL = [
    ("import", import_time),
    ("csv.read", csv_read),
    ("transforms", transforms),
    ("sqlite.write", sqlite_write),
    ("postgres.upsert", postgres_upsert),
    ("http.get fan-out", http_fanout),
    ("agent step", agent_step),
    ("agent cache re-run", agent_cache),
]
