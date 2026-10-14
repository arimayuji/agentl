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
    """Fixed latency and a simple token model: measures agentl's overhead, concurrency and how the
    call shape changes tokens, not a model. Each call pays a 400-token prefix (system, instruction,
    schema) plus 60 input and 40 output tokens per record. With `options.prompt_cache` the prefix
    is reported as a cache read after the first call, the way providers do."""

    latency = 0.02
    prefix, per_in, per_out = 400, 60, 40

    def __init__(self, spec):
        self.cache = bool(spec.options.get("prompt_cache"))
        self.warm = False

    def structured(self, instruction, record, schema, sleep=True):
        if sleep:
            time.sleep(self.latency)
        recs = record.get("records")
        n = len(recs) if recs is not None else 1
        tin, tout = self.prefix + n * self.per_in, n * self.per_out
        read = self.prefix if self.cache and self.warm else 0
        self.warm = True
        answer = {"category": "software", "ok": True}
        if recs is None:
            return Result(schema.model_validate(answer), tin, tout, read)
        items = [{"index": r["index"], **answer} for r in recs]
        return Result(schema.model_validate({"items": items}), tin, tout, read)

    # Batch mode: answered at once. A real batch takes minutes to hours; that is not modelled.
    def submit_batch(self, instruction, records, schema):
        self.pending = {
            cid: self.structured(instruction, r, schema, sleep=False) for cid, r in records.items()
        }
        return "bench-batch"

    def batch_done(self, batch_id):
        return True

    def batch_results(self, batch_id, schema):
        return self.pending

    def text(self, instruction, content):
        return ""


register_provider("bench-fake", _FakeLLM)


def _agent_spec(concurrency: int, per_call: int = 1, prompt_cache: bool = False, mode: str = "sync"):
    price = {"input": 3.0, "output": 15.0, "cache_read": 0.3, "batch": 0.5}
    opts = {"prompt_cache": True} if prompt_cache else {}
    return parse(
        {
            "name": "bench",
            "llm": {"provider": "bench-fake", "model": "m", "price": price, "options": opts},
            "steps": [
                {"id": "src", "fn": "benchmarks.scenarios:_rows"},
                {
                    "id": "classify",
                    "agent": "Classify.",
                    "input": "src",
                    "concurrency": concurrency,
                    "records_per_call": per_call,
                    "mode": mode,
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


def agent_call_shapes(scale: float) -> dict:
    """Same 500 records, concurrency 8: default, prompt caching, 20 records per call, batch."""
    global _N_AGENT
    _N_AGENT = int(500 * scale)
    d = _tmp()
    out = {}
    for name, kw in (
        ("default", {}),
        ("prompt_cache", {"prompt_cache": True}),
        ("per_call_20", {"per_call": 20}),
        ("batch", {"mode": "batch"}),
    ):
        with Store(d / f"{name}.db") as store:
            r, s = _timed(
                lambda kw=kw, store=store: Runner(_agent_spec(8, **kw), store=store, use_cache=False).run()
            )
        assert r.status == "succeeded", r.error
        out[name] = {
            "calls": r.llm_calls,
            "tokens": r.input_tokens + r.output_tokens,
            "cached_tokens": r.cache_read_tokens,
            "cost_usd": round(r.cost_usd, 4),
            "seconds": round(s, 2),
        }
    base, cache, many = (
        out["default"]["cost_usd"],
        out["prompt_cache"]["cost_usd"],
        out["per_call_20"]["cost_usd"],
    )
    batch = out["batch"]["cost_usd"]
    return {
        "summary": (
            f"{base} USD; prompt cache {cache} ({base / cache:.1f}x less), "
            f"20 per call {many} ({base / many:.1f}x less), batch {batch} ({base / batch:.1f}x less)"
        ),
        "records": _N_AGENT,
        **out,
    }


# ---------------------------------------------------------------- parallel steps
def _slow_source(input=None, seconds=0.5):
    time.sleep(seconds)
    return [{"i": i} for i in range(10)]


def parallel_steps(scale: float) -> dict:
    """Two independent sources of 0.5 s each and a join: sequential vs parallel."""
    src = {"fn": "benchmarks.scenarios:_slow_source"}
    steps = [
        {"id": "invoices", **src},
        {"id": "customers", **src},
        {"id": "joined", "use": "select", "input": "invoices"},
    ]
    out = {}
    d = _tmp()
    for n in (1, 4):
        spec = parse({"name": "par", "max_parallel_steps": n, "steps": steps})
        with Store(d / f"p{n}.db") as store:
            r, s = _timed(lambda spec=spec, store=store: Runner(spec, store=store).run())
        assert r.status == "succeeded", r.error
        out[f"max_parallel_{n}_s"] = round(s, 2)
    return {
        "summary": f"{out['max_parallel_1_s']} s one at a time, {out['max_parallel_4_s']} s in parallel",
        "source_seconds": 0.5,
        **out,
    }


# ---------------------------------------------------------------- chunked execution
_CHUNK_CHILD = """
import resource, sys, time
from agentl import parse
from agentl.runner import Runner
from agentl.store import Store
src, db, chunk = sys.argv[1], sys.argv[2], sys.argv[3]
base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
spec = parse({
    "name": "chunk", "chunk_size": int(chunk) or None,
    "steps": [
        {"id": "read", "use": "csv.read", "with": {"path": src}},
        {"id": "slim", "use": "select", "input": "read", "with": {"fields": ["id", "name", "amount"]}},
        {"id": "save", "use": "sqlite.write", "input": "slim",
         "with": {"db": db, "table": "t", "mode": "replace"}},
    ],
})
t0 = time.perf_counter()
with Store(db + ".state") as store:
    r = Runner(spec, store=store, use_cache=False).run()
assert r.status == "succeeded", r.error
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
scale = 1 if sys.platform == "darwin" else 1024  # macOS reports bytes, Linux KiB
print((peak - base) * scale / 2**20, time.perf_counter() - t0)
"""


def chunked(scale: float) -> dict:
    """csv.read -> select -> sqlite.write on a large CSV, all at once vs 50k records at a time.
    Each mode runs in a fresh process; memory is the growth of its peak RSS during the run."""
    try:
        import resource  # noqa: F401
    except ImportError as e:
        raise Skip("needs the resource module (Linux or macOS)") from e
    n = int(1_000_000 * scale)
    d = _tmp()
    src = d / "big.csv"
    with open(src, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "name", "amount", "notes"])
        for i in range(n):
            w.writerow([i, f"customer {i % 5000}", f"{i * 1.5:.2f}", "x" * 40])
    out = {"records": n, "csv_mb": round(src.stat().st_size / 2**20, 1)}
    for label, chunk in (("all_at_once", 0), ("chunk_50k", 50_000)):
        res = subprocess.run(
            [sys.executable, "-c", _CHUNK_CHILD, str(src), str(d / f"{label}.db"), str(chunk)],
            capture_output=True,
            text=True,
            check=True,
        )
        mb, s = map(float, res.stdout.split())
        out[f"{label}_mb"] = round(mb)
        out[f"{label}_s"] = round(s, 2)
    return {
        "summary": f"peak +{out['all_at_once_mb']} MB all at once, +{out['chunk_50k_mb']} MB in 50k chunks",
        **out,
    }


# ---------------------------------------------------------------- arrow fast path
def _need_arrow() -> None:
    import importlib.util

    for mod in ("pyarrow", "pandas"):
        if importlib.util.find_spec(mod) is None:
            raise Skip(f"needs {mod}: pip install 'agentl-etl[arrow,pandas]'")


def arrow_csv_pandas_parquet(scale: float) -> dict:
    """csv.read -> @dataframe -> parquet.write with `arrow: false` and with the Arrow fast path."""
    _need_arrow()
    import pyarrow.parquet as pq

    n = int(500_000 * scale)
    d = _tmp()
    src = d / "in.csv"
    with open(src, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "kind", "amount", "note"])
        for i in range(n):
            w.writerow([i, "ab"[i % 2], f"{i * 1.37:.3f}", "x" * 20])
    out = {"records": n}
    for mode in (False, True):
        target = d / f"out-{mode}.parquet"
        spec = parse(
            {
                "name": "arrow",
                "arrow": mode,
                "steps": [
                    {"id": "read", "use": "csv.read", "with": {"path": str(src)}},
                    {"id": "clean", "fn": "benchmarks.frames:clean", "input": "read"},
                    {"id": "save", "use": "parquet.write", "input": "clean", "with": {"path": str(target)}},
                ],
            }
        )
        with Store(d / f"s-{mode}.db") as store:
            r, s = _timed(lambda spec=spec, store=store: Runner(spec, store=store).run())
        assert r.status == "succeeded", r.error
        out["arrow_s" if mode else "dicts_s"] = round(s, 2)
    same = pq.read_table(d / "out-True.parquet").equals(pq.read_table(d / "out-False.parquet"))
    assert same, "Arrow and dict paths wrote different Parquet"
    return {
        "summary": f"{out['dicts_s'] / out['arrow_s']:.1f}x faster with Arrow, same Parquet",
        "identical": same,
        **out,
    }


def arrow_parquet_postgres(scale: float) -> dict:
    """parquet.read -> postgres.write (append): executemany from records vs COPY from Arrow."""
    _need_arrow()
    dsn = os.environ.get("AGENTL_TEST_PG_DSN")
    if not dsn:
        raise Skip("set AGENTL_TEST_PG_DSN")
    import psycopg
    import pyarrow as pa
    import pyarrow.parquet as pq

    n = int(200_000 * scale)
    d = _tmp()
    src = d / "in.parquet"
    rows = [{"id": i, "name": f"n{i}", "total": i * 0.5, "ok": i % 3 == 0} for i in range(n)]
    pq.write_table(pa.Table.from_pylist(rows), src)
    tables = {mode: f"bench_arrow_{mode}_{random.randint(0, 1 << 30)}".lower() for mode in (False, True)}
    out = {"records": n}
    try:
        for mode, table in tables.items():
            spec = parse(
                {
                    "name": "pg",
                    "arrow": mode,
                    "steps": [
                        {"id": "read", "use": "parquet.read", "with": {"path": str(src)}},
                        {
                            "id": "save",
                            "use": "postgres.write",
                            "input": "read",
                            "with": {"dsn": dsn, "table": table},
                        },
                    ],
                }
            )
            with Store(d / f"s-{mode}.db") as store:
                r, s = _timed(lambda spec=spec, store=store: Runner(spec, store=store).run())
            assert r.status == "succeeded", r.error
            out["copy_s" if mode else "insert_s"] = round(s, 2)
        with psycopg.connect(dsn) as conn:
            a, b = (conn.execute(f"select * from {t} order by id").fetchall() for t in tables.values())
        assert a == b, "COPY and INSERT loaded different rows"
    finally:
        with psycopg.connect(dsn) as conn:
            for t in tables.values():
                conn.execute(f"drop table if exists {t}")
    return {
        "summary": f"{out['insert_s'] / out['copy_s']:.1f}x faster with COPY from Arrow, same rows",
        **out,
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
    ("agent call shapes", agent_call_shapes),
    ("parallel steps", parallel_steps),
    ("chunked", chunked),
    ("arrow csv-pandas-parquet", arrow_csv_pandas_parquet),
    ("arrow parquet-postgres", arrow_parquet_postgres),
]
