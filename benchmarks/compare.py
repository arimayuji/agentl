"""The same job written with agentl and without it, measured the same way.

Each variant runs in a fresh Python process that records wall time, CPU time (user + system),
peak memory growth (RSS) and I/O through system calls (Linux `/proc/self/io` rchar/wchar). Every
variant writes its result to a file, and the comparison fails unless all variants of a case wrote
the same data. The plain variants do not import agentl.

    python -m benchmarks --compare [--quick] [--json out.json]
"""

from __future__ import annotations

import csv
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

LLM_LATENCY = 0.02
ANSWER = {"category": "software", "ok": True}


class Skip(Exception):
    pass


def _has(*mods: str) -> bool:
    import importlib.util

    return all(importlib.util.find_spec(m) is not None for m in mods)


# ---------------------------------------------------------------- case 1: CSV -> clean -> SQLite
def _make_csv(d: Path, n: int) -> Path:
    src = d / "orders.csv"
    with open(src, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "customer", "amount", "notes"])
        for i in range(n):
            w.writerow([i, f"customer {i % 5000}", f"{i * 1.5:.2f}", "" if i % 10 == 0 else "x" * 40])
    return src


def sqlite_agentl(d: Path) -> Callable[[], None]:
    from agentl import parse
    from agentl.runner import Runner
    from agentl.store import Store

    spec = parse(
        {
            "name": "orders",
            "chunk_size": 50_000,
            "steps": [
                {"id": "read", "use": "csv.read", "with": {"path": str(d / "orders.csv")}},
                {"id": "keep", "use": "filter", "input": "read", "with": {"not_empty": "notes"}},
                {
                    "id": "slim",
                    "use": "select",
                    "input": "keep",
                    "with": {"fields": ["id", "customer", "amount"]},
                },
                {
                    "id": "save",
                    "use": "sqlite.write",
                    "input": "slim",
                    "with": {"db": str(d / "out.db"), "table": "orders", "mode": "replace"},
                },
            ],
        }
    )

    def run() -> None:
        with Store(d / "state.db") as store:
            r = Runner(spec, store=store, use_cache=False).run()
        assert r.status == "succeeded", r.error

    return run


def sqlite_plain(d: Path) -> Callable[[], None]:
    """What a careful developer writes by hand: stream the CSV, insert in batches of 50k."""
    import sqlite3

    def run() -> None:
        conn = sqlite3.connect(d / "out.db")
        with conn:
            conn.execute('DROP TABLE IF EXISTS "orders"')
            conn.execute('CREATE TABLE "orders" ("id", "customer", "amount")')
            batch: list[tuple] = []
            with open(d / "orders.csv", newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row["notes"]:
                        batch.append((row["id"], row["customer"], row["amount"]))
                    if len(batch) == 50_000:
                        conn.executemany('INSERT INTO "orders" VALUES (?, ?, ?)', batch)
                        batch.clear()
            conn.executemany('INSERT INTO "orders" VALUES (?, ?, ?)', batch)
        conn.close()

    return run


def sqlite_pandas(d: Path) -> Callable[[], None]:
    import sqlite3

    import pandas as pd

    def run() -> None:
        df = pd.read_csv(d / "orders.csv", dtype=str, keep_default_na=False)
        df = df[df["notes"] != ""][["id", "customer", "amount"]]
        conn = sqlite3.connect(d / "out.db")
        df.to_sql("orders", conn, if_exists="replace", index=False)
        conn.close()

    return run


def sqlite_result(d: Path) -> list:
    import sqlite3

    conn = sqlite3.connect(d / "out.db")
    rows = conn.execute('SELECT id, customer, amount FROM "orders"').fetchall()
    conn.close()
    return sorted((str(a), str(b), str(c)) for a, b, c in rows)


# ---------------------------------------------------------------- case 2: one LLM call per record
def _llm_rows(input=None) -> list[dict]:
    return [{"i": i, "description": f"invoice line {i}"} for i in range(int(os.environ["BENCH_N"]))]


def _fake_llm(record: dict) -> dict:
    time.sleep(LLM_LATENCY)
    return dict(ANSWER)


def _llm_spec(d: Path):
    from agentl import parse

    from . import scenarios  # registers the bench-fake provider (20 ms per call)

    assert scenarios._FakeLLM.latency == LLM_LATENCY
    return parse(
        {
            "name": "enrich",
            "llm": {"provider": "bench-fake", "model": "m", "price": {"input": 3.0, "output": 15.0}},
            "steps": [
                {"id": "src", "fn": "benchmarks.compare:_llm_rows"},
                {
                    "id": "classify",
                    "agent": "Classify.",
                    "input": "src",
                    "concurrency": 8,
                    "output": {"category": "str", "ok": "bool"},
                },
                {
                    "id": "save",
                    "use": "json.write",
                    "input": "classify",
                    "with": {"path": str(d / "out.jsonl")},
                },
            ],
        }
    )


def llm_agentl(d: Path) -> Callable[[], None]:
    from agentl.runner import Runner
    from agentl.store import Store

    spec = _llm_spec(d)

    def run() -> None:
        with Store(d / "state.db") as store:
            r = Runner(spec, store=store, use_cache=False).run()
        assert r.status == "succeeded", r.error

    return run


def llm_agentl_rerun(d: Path) -> Callable[[], None]:
    """The second run of the same pipeline on the same data: every answer comes from the cache."""
    from agentl.runner import Runner
    from agentl.store import Store

    spec = _llm_spec(d)
    with Store(d / "state.db") as store:  # first run, not measured
        assert Runner(spec, store=store).run().status == "succeeded"

    def run() -> None:
        with Store(d / "state.db") as store:
            r = Runner(spec, store=store).run()
        assert r.status == "succeeded" and r.llm_calls == 0, r.error

    return run


def _write_jsonl(d: Path, rows: list[dict]) -> None:
    with open(d / "out.jsonl", "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def llm_plain_loop(d: Path) -> Callable[[], None]:
    def run() -> None:
        _write_jsonl(d, [{**r, **_fake_llm(r)} for r in _llm_rows()])

    return run


def llm_plain_threads(d: Path) -> Callable[[], None]:
    from concurrent.futures import ThreadPoolExecutor

    def run() -> None:
        rows = _llm_rows()
        with ThreadPoolExecutor(8) as pool:
            answers = list(pool.map(_fake_llm, rows))
        _write_jsonl(d, [{**r, **a} for r, a in zip(rows, answers, strict=True)])

    return run


def llm_result(d: Path) -> list:
    with open(d / "out.jsonl", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f]
    return sorted((r["i"], r["description"], r["category"], r["ok"]) for r in rows)


# ---------------------------------------------------------------- case 3: CSV -> pandas -> Parquet
def _make_frames_csv(d: Path, n: int) -> Path:
    src = d / "in.csv"
    with open(src, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "kind", "amount", "note"])
        for i in range(n):
            w.writerow([i, "ab"[i % 2], f"{i * 1.37:.3f}", "x" * 20])
    return src


def parquet_agentl(d: Path) -> Callable[[], None]:
    from agentl import parse
    from agentl.runner import Runner
    from agentl.store import Store

    spec = parse(
        {
            "name": "frames",
            "steps": [
                {"id": "read", "use": "csv.read", "with": {"path": str(d / "in.csv")}},
                {"id": "clean", "fn": "benchmarks.frames:clean", "input": "read"},
                {
                    "id": "save",
                    "use": "parquet.write",
                    "input": "clean",
                    "with": {"path": str(d / "out.parquet")},
                },
            ],
        }
    )

    def run() -> None:
        with Store(d / "state.db") as store:
            r = Runner(spec, store=store, use_cache=False).run()
        assert r.status == "succeeded", r.error

    return run


def parquet_pandas(d: Path) -> Callable[[], None]:
    import pandas as pd

    def run() -> None:
        df = pd.read_csv(d / "in.csv", dtype=str, keep_default_na=False)
        df = df[df["kind"] == "a"].copy()
        df["amount"] = df["amount"].astype(float).round(1)
        df.to_parquet(d / "out.parquet", index=False)

    return run


def parquet_result(d: Path) -> list:
    import pyarrow.parquet as pq

    t = pq.read_table(d / "out.parquet", columns=["id", "kind", "amount", "note"])
    return sorted(tuple(r.values()) for r in t.to_pylist())


# ---------------------------------------------------------------- cases
CASES: dict[str, dict] = {
    "csv -> clean -> sqlite": {
        "rows": 1_000_000,
        "make": _make_csv,
        "result": sqlite_result,
        "needs": (),
        "variants": {
            "agentl (chunk_size 50k)": (sqlite_agentl, ()),
            "plain Python (csv + sqlite3, batches)": (sqlite_plain, ()),
            "pandas (read_csv + to_sql)": (sqlite_pandas, ("pandas",)),
        },
    },
    "LLM call per record": {
        "rows": 500,
        "make": None,
        "result": llm_result,
        "needs": (),
        "variants": {
            "agentl (concurrency 8)": (llm_agentl, ()),
            "agentl re-run (cache)": (llm_agentl_rerun, ()),
            "plain Python (loop)": (llm_plain_loop, ()),
            "plain Python (8 threads)": (llm_plain_threads, ()),
        },
    },
    "csv -> pandas -> parquet": {
        "rows": 500_000,
        "make": _make_frames_csv,
        "result": parquet_result,
        "needs": ("pandas", "pyarrow"),
        "variants": {
            "agentl (@dataframe, Arrow)": (parquet_agentl, ()),
            "pandas only": (parquet_pandas, ()),
        },
    },
}


# ---------------------------------------------------------------- measurement (child process)
def _io() -> tuple[int, int]:
    try:
        with open("/proc/self/io") as f:
            vals = dict(line.split(": ") for line in f.read().splitlines())
        return int(vals["rchar"]), int(vals["wchar"])
    except OSError:
        return -1, -1


def _child(case: str, variant: str, workdir: str) -> None:
    import resource

    d = Path(workdir)
    fn, _ = CASES[case]["variants"][variant]
    run = fn(d)  # imports and preparation happen here, outside the measurement
    usage = resource.getrusage(resource.RUSAGE_SELF)
    rss0, cpu0, (r0, w0) = usage.ru_maxrss, usage.ru_utime + usage.ru_stime, _io()
    t0 = time.perf_counter()
    run()
    wall = time.perf_counter() - t0
    usage = resource.getrusage(resource.RUSAGE_SELF)
    r1, w1 = _io()
    unit = 1 if sys.platform == "darwin" else 1024  # macOS reports bytes, Linux KiB
    print(
        json.dumps(
            {
                "seconds": wall,
                "cpu_s": usage.ru_utime + usage.ru_stime - cpu0,
                "peak_mb": (usage.ru_maxrss - rss0) * unit / 2**20,
                "read_mb": (r1 - r0) / 2**20 if r0 >= 0 else None,
                "written_mb": (w1 - w0) / 2**20 if w0 >= 0 else None,
                "imports_agentl": "agentl" in sys.modules,
            }
        )
    )


def _measure(case: str, variant: str, d: Path, n: int) -> dict:
    env = {**os.environ, "BENCH_N": str(n)}
    for f in ("out.db", "state.db", "out.jsonl", "out.parquet"):
        (d / f).unlink(missing_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", "benchmarks.compare", case, variant, str(d)],
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parent.parent,
    )
    if proc.returncode:
        raise RuntimeError(f"{case} / {variant} failed:\n{proc.stderr[-2000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def run_all(scale: float, repeat: int) -> list[dict]:
    try:
        import resource  # noqa: F401
    except ImportError as e:
        raise SystemExit("--compare needs the resource module (Linux or macOS)") from e
    out = []
    for case, c in CASES.items():
        if not _has(*c["needs"]):
            out.append({"case": case, "skipped": f"needs {', '.join(c['needs'])}"})
            continue
        n = max(10, int(c["rows"] * scale))
        d = Path(tempfile.mkdtemp(prefix="agentl-compare-"))
        if c["make"]:
            c["make"](d, n)
        results, reference = [], None
        for variant, (_, needs) in c["variants"].items():
            if not _has(*needs):
                results.append({"variant": variant, "skipped": f"needs {', '.join(needs)}"})
                continue
            print(f"… {case} / {variant}", file=sys.stderr, flush=True)
            runs = [_measure(case, variant, d, n) for _ in range(repeat)]
            data = c["result"](d)
            if reference is None:
                reference = data
            if data != reference:
                raise AssertionError(f"{case}: '{variant}' wrote different data from the first variant")
            best = {k: statistics.median(r[k] for r in runs) for k in ("seconds", "cpu_s", "peak_mb")}
            last = runs[-1]
            results.append(
                {
                    "variant": variant,
                    **{k: round(v, 3) for k, v in best.items()},
                    "read_mb": None if last["read_mb"] is None else round(last["read_mb"], 1),
                    "written_mb": None if last["written_mb"] is None else round(last["written_mb"], 1),
                    "imports_agentl": last["imports_agentl"],
                }
            )
        out.append({"case": case, "rows": n, "identical_output": True, "variants": results})
    return out


def table(results: list[dict]) -> str:
    lines = []
    for c in results:
        if "skipped" in c:
            lines.append(f"\n{c['case']}: skipped ({c['skipped']})")
            continue
        lines.append(f"\n{c['case']} ({c['rows']:,} rows, identical output in every variant)")
        lines.append(
            f"  {'variant':<38} {'seconds':>8} {'cpu s':>7} {'peak MB':>8} {'read MB':>8} {'written MB':>10}"
        )
        for v in c["variants"]:
            if "skipped" in v:
                lines.append(f"  {v['variant']:<38} skipped: {v['skipped']}")
                continue
            io = [("n/a" if v[k] is None else f"{v[k]:.1f}") for k in ("read_mb", "written_mb")]
            lines.append(
                f"  {v['variant']:<38} {v['seconds']:>8.2f} {v['cpu_s']:>7.2f} {v['peak_mb']:>8.0f}"
                f" {io[0]:>8} {io[1]:>10}"
            )
    return "\n".join(lines)


if __name__ == "__main__":
    _child(*sys.argv[1:4])
