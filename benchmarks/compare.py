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
from typing import Any

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


# ---------------------------------------------------------------- cases 4 and 5: HTTP APIs
API_LATENCY = 0.01  # seconds per request, like a fast real API
PAGE = 100  # items per page


def _api() -> str:
    """A local JSON API with a fixed latency per request, started inside the measured process (its
    CPU counts the same for every variant): /items?page=N (PAGE items, `next_page` null at the end)
    and /items/<id>."""
    import http.server
    import threading

    n = int(os.environ["BENCH_N"])

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive, as real APIs

        def setup(self) -> None:
            super().setup()
            # Headers and body go out in separate writes: without TCP_NODELAY, Nagle plus delayed
            # ACKs add ~40 ms per request on keep-alive clients (requests, httpx) and not on
            # urllib, which closes each connection. That would measure the test server.
            import socket

            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        def log_message(self, *a: object) -> None:
            pass

        def do_GET(self) -> None:
            time.sleep(API_LATENCY)
            path, _, query = self.path.partition("?")
            if path == "/items":
                page = int(dict(p.split("=") for p in query.split("&") if p).get("page", 1))
                lo = (page - 1) * PAGE
                items = [
                    {"id": i, "sku": f"SKU-{i:05d}", "stock": i % 37} for i in range(lo, min(lo + PAGE, n))
                ]
                body: dict[str, Any] = {"data": items, "next_page": page + 1 if lo + PAGE < n else None}
            else:
                i = int(path.rsplit("/", 1)[1])
                body = {"id": i, "price": round(i * 1.25, 2), "currency": "BRL"}
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["AGENTL_ALLOW_HOSTS"] = "127.0.0.1"  # agentl's SSRF guard, for the local server
    return f"http://127.0.0.1:{srv.server_address[1]}"


def _agentl_run(d: Path, steps: list[dict]) -> Callable[[], None]:
    from agentl import parse
    from agentl.runner import Runner
    from agentl.store import Store

    spec = parse({"name": "bench", "steps": steps})

    def run() -> None:
        with Store(d / "state.db") as store:
            r = Runner(spec, store=store, use_cache=False).run()
        assert r.status == "succeeded", r.error

    return run


def pages_agentl(d: Path) -> Callable[[], None]:
    base = _api()
    return _agentl_run(
        d,
        [
            {
                "id": "items",
                "use": "http.get",
                "with": {
                    "url": f"{base}/items",
                    "items": "data",
                    "paginate": {"param": "page", "next": "next_page"},
                },
            },
            {"id": "save", "use": "json.write", "input": "items", "with": {"path": str(d / "out.jsonl")}},
        ],
    )


def pages_urllib(d: Path) -> Callable[[], None]:
    import urllib.request

    base = _api()

    def run() -> None:
        out, page = [], 1
        while page:
            with urllib.request.urlopen(f"{base}/items?page={page}") as resp:
                body = json.load(resp)
            out += body["data"]
            page = body["next_page"]
        _write_jsonl(d, out)

    return run


def pages_requests(d: Path) -> Callable[[], None]:
    import requests

    base = _api()

    def run() -> None:
        out, page = [], 1
        with requests.Session() as s:
            while page:
                body = s.get(f"{base}/items", params={"page": page}).json()
                out += body["data"]
                page = body["next_page"]
        _write_jsonl(d, out)

    return run


def pages_httpx(d: Path) -> Callable[[], None]:
    import httpx

    base = _api()

    def run() -> None:
        out, page = [], 1
        with httpx.Client() as c:
            while page:
                body = c.get(f"{base}/items", params={"page": page}).json()
                out += body["data"]
                page = body["next_page"]
        _write_jsonl(d, out)

    return run


def _rows_from(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def pages_result(d: Path) -> list:
    return sorted((r["id"], r["sku"], r["stock"]) for r in _rows_from(d / "out.jsonl"))


def _ids(input=None) -> list[dict]:
    return [{"id": i} for i in range(int(os.environ["BENCH_N"]))]


def fanout_agentl(d: Path) -> Callable[[], None]:
    base = _api()
    return _agentl_run(
        d,
        [
            {"id": "ids", "fn": "benchmarks.compare:_ids"},
            {
                "id": "prices",
                "use": "http.get",
                "input": "ids",
                "with": {"url": base + "/items/{id}", "concurrency": 16},
            },
            {"id": "save", "use": "json.write", "input": "prices", "with": {"path": str(d / "out.jsonl")}},
        ],
    )


def fanout_requests_loop(d: Path) -> Callable[[], None]:
    import requests

    base = _api()

    def run() -> None:
        with requests.Session() as s:
            _write_jsonl(d, [s.get(f"{base}/items/{r['id']}").json() for r in _ids()])

    return run


def fanout_requests_threads(d: Path) -> Callable[[], None]:
    from concurrent.futures import ThreadPoolExecutor

    import requests

    base = _api()

    def run() -> None:
        def one(r: dict) -> dict:
            return requests.get(f"{base}/items/{r['id']}").json()

        with ThreadPoolExecutor(16) as pool:
            _write_jsonl(d, list(pool.map(one, _ids())))

    return run


def fanout_httpx_async(d: Path) -> Callable[[], None]:
    import asyncio

    import httpx

    base = _api()

    def run() -> None:
        async def main() -> list[dict]:
            sem = asyncio.Semaphore(16)
            async with httpx.AsyncClient() as c:

                async def one(r: dict) -> dict:
                    async with sem:
                        return (await c.get(f"{base}/items/{r['id']}")).json()

                return await asyncio.gather(*(one(r) for r in _ids()))

        _write_jsonl(d, asyncio.run(main()))

    return run


def fanout_result(d: Path) -> list:
    return sorted((r["id"], r["price"], r["currency"]) for r in _rows_from(d / "out.jsonl"))


# ---------------------------------------------------------------- case 6: HTML table -> CSV
def _make_html(d: Path, n: int) -> Path:
    rows = "".join(
        f"<tr><td>Plan {i}</td><td>{i % 50 + 1}</td><td>R$ {i * 9.9:.2f}</td></tr>" for i in range(n)
    )
    page = (
        "<html><body><nav>menu</nav><table><thead><tr><th>Plan</th><th>Seats</th><th>Price</th></tr></thead>"
        f"<tbody>{rows}</tbody></table><footer>footer</footer></body></html>"
    )
    p = d / "page.html"
    p.write_text(page, encoding="utf-8")
    return p


def html_agentl(d: Path) -> Callable[[], None]:
    return _agentl_run(
        d,
        [
            {"id": "table", "use": "html.read", "with": {"path": str(d / "page.html")}},
            {"id": "save", "use": "csv.write", "input": "table", "with": {"path": str(d / "out.csv")}},
        ],
    )


def html_bs4(d: Path) -> Callable[[], None]:
    from bs4 import BeautifulSoup

    def run() -> None:
        soup = BeautifulSoup((d / "page.html").read_text(encoding="utf-8"), "html.parser")
        table: Any = soup.find("table")
        head = [th.get_text(strip=True) for th in table.find("thead").find_all("th")]
        with open(d / "out.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(head)
            for tr in table.find("tbody").find_all("tr"):
                w.writerow([td.get_text(strip=True) for td in tr.find_all("td")])

    return run


def html_bs4_lxml(d: Path) -> Callable[[], None]:
    from bs4 import BeautifulSoup

    def run() -> None:
        soup = BeautifulSoup((d / "page.html").read_text(encoding="utf-8"), "lxml")
        table: Any = soup.find("table")
        head = [th.get_text(strip=True) for th in table.find("thead").find_all("th")]
        with open(d / "out.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(head)
            for tr in table.find("tbody").find_all("tr"):
                w.writerow([td.get_text(strip=True) for td in tr.find_all("td")])

    return run


def html_pandas(d: Path) -> Callable[[], None]:
    import io

    import pandas as pd

    def run() -> None:
        df = pd.read_html(io.StringIO((d / "page.html").read_text(encoding="utf-8")))[0].astype(str)
        df.to_csv(d / "out.csv", index=False)

    return run


def html_result(d: Path) -> list:
    with open(d / "out.csv", newline="", encoding="utf-8") as f:
        return sorted(tuple(r[k] for k in ("Plan", "Seats", "Price")) for r in csv.DictReader(f))


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
    "paginated API -> JSON": {
        "rows": 10_000,  # 100 pages of 100 items, 10 ms per request
        "make": None,
        "result": pages_result,
        "needs": (),
        "variants": {
            "agentl (http.get, paginate)": (pages_agentl, ()),
            "urllib (stdlib loop)": (pages_urllib, ()),
            "requests (Session loop)": (pages_requests, ("requests",)),
            "httpx (Client loop)": (pages_httpx, ("httpx",)),
        },
    },
    "one request per record": {
        "rows": 500,  # 500 GETs, 10 ms each
        "make": None,
        "result": fanout_result,
        "needs": (),
        "variants": {
            "agentl (http.get, concurrency 16)": (fanout_agentl, ()),
            "requests (Session loop)": (fanout_requests_loop, ("requests",)),
            "requests (16 threads)": (fanout_requests_threads, ("requests",)),
            "httpx (async, 16 at a time)": (fanout_httpx_async, ("httpx",)),
        },
    },
    "HTML table -> CSV": {
        "rows": 20_000,
        "make": _make_html,
        "result": html_result,
        "needs": (),
        "variants": {
            "agentl (html.read)": (html_agentl, ()),
            "BeautifulSoup (html.parser)": (html_bs4, ("bs4",)),
            "BeautifulSoup (lxml)": (html_bs4_lxml, ("bs4", "lxml")),
            "pandas (read_html)": (html_pandas, ("pandas", "lxml")),
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


# ---------------------------------------------------------------- regression gate
# What the gate compares: agentl against a reference variant measured in the same run, on the same
# machine. A slower CI runner slows both, so the ratio stays put; only agentl getting slower moves it.
# (case, agentl variant, reference variant, time tolerance). The Parquet job takes half a second, so
# its ratio swings by tens of percent between runs on a shared machine: it gets a wider tolerance.
GATES: list[tuple[str, str, str, float]] = [
    ("csv -> clean -> sqlite", "agentl (chunk_size 50k)", "plain Python (csv + sqlite3, batches)", 0.2),
    ("LLM call per record", "agentl (concurrency 8)", "plain Python (8 threads)", 0.2),
    ("csv -> pandas -> parquet", "agentl (@dataframe, Arrow)", "pandas only", 0.75),
    ("one request per record", "agentl (http.get, concurrency 16)", "requests (16 threads)", 0.25),
    ("HTML table -> CSV", "agentl (html.read)", "BeautifulSoup (lxml)", 0.25),
]


def ratios(results: list[dict]) -> dict[str, dict]:
    """Per gate: agentl seconds over the reference's, and agentl's peak MB."""
    by_case = {
        c["case"]: {v["variant"]: v for v in c.get("variants", []) if "skipped" not in v} for c in results
    }
    out: dict[str, dict] = {}
    for case, mine, ref, tolerance in GATES:
        v = by_case.get(case, {})
        if mine in v and ref in v and v[ref]["seconds"] > 0:
            out[case] = {
                "agentl": mine,
                "reference": ref,
                "time_ratio": round(v[mine]["seconds"] / v[ref]["seconds"], 3),
                "peak_mb": round(v[mine]["peak_mb"], 1),
                "time_tolerance": tolerance,
            }
    return out


def check(results: list[dict], baseline: dict, mb_tolerance: float = 0.25) -> list[str]:
    """Regressions against `baseline` (a file written by --write-baseline): a time ratio more than
    its gate's tolerance above the baseline's, or a peak memory more than `mb_tolerance` above it
    (plus 10 MB of slack for small numbers). An empty list means the gate passes."""
    now = ratios(results)
    problems = []
    for case, base in baseline.get("ratios", {}).items():
        cur = now.get(case)
        if cur is None:
            problems.append(f"{case}: not measured (a variant was skipped or renamed)")
            continue
        limit = base["time_ratio"] * (1 + cur["time_tolerance"])
        if cur["time_ratio"] > limit:
            problems.append(
                f"{case}: agentl takes {cur['time_ratio']:.2f}x the reference, baseline "
                f"{base['time_ratio']:.2f}x (limit {limit:.2f}x)"
            )
        mb_limit = base["peak_mb"] * (1 + mb_tolerance) + 10
        if cur["peak_mb"] > mb_limit:
            problems.append(
                f"{case}: peak {cur['peak_mb']:.0f} MB, "
                f"baseline {base['peak_mb']:.0f} MB (limit {mb_limit:.0f})"
            )
    return problems


def check_against(results: list[dict], mb_tolerance: float = 0.25) -> tuple[dict[str, dict], list[str]]:
    """Regressions of this agentl against the one measured as `<variant> @ base` on the same machine
    (run_all's `against`): time more than its gate's tolerance above the base's, or peak memory more
    than `mb_tolerance` (plus 10 MB) above it. Returns the per-gate numbers and the problems."""
    by_case = {
        c["case"]: {v["variant"]: v for v in c.get("variants", []) if "skipped" not in v} for c in results
    }
    rows: dict[str, dict] = {}
    problems = []
    ran = {c["case"] for c in results}
    base_skipped = {
        (c["case"], v["variant"]) for c in results for v in c.get("variants", []) if "skipped" in v
    }
    for case, mine, _, tolerance in GATES:
        if case not in ran:  # left out with --only
            continue
        v = by_case.get(case, {})
        if mine in v and (case, mine + BASE) in base_skipped:
            rows[case] = {"note": "new in this change: the base cannot run it"}
            continue
        if mine not in v or mine + BASE not in v:
            problems.append(f"{case}: not measured against the base (a variant was skipped or renamed)")
            continue
        head, base = v[mine], v[mine + BASE]
        ratio = head["seconds"] / base["seconds"] if base["seconds"] > 0 else 1.0
        # Whole-process peak RSS, not growth over setup: growth reads 0 or the full amount from run to
        # run, depending on whether setup had already pushed the high-water mark past it.
        rows[case] = {
            "time_ratio": round(ratio, 3),
            "rss_mb": head["max_rss_mb"],
            "base_rss_mb": base["max_rss_mb"],
        }
        if ratio > 1 + tolerance:
            problems.append(
                f"{case}: {head['seconds']:.2f} s, base {base['seconds']:.2f} s "
                f"({ratio:.2f}x, limit {1 + tolerance:.2f}x)"
            )
        mb_limit = base["max_rss_mb"] * (1 + mb_tolerance) + 10
        if head["max_rss_mb"] > mb_limit:
            problems.append(
                f"{case}: peak RSS {head['max_rss_mb']:.0f} MB, base {base['max_rss_mb']:.0f} MB "
                f"(limit {mb_limit:.0f})"
            )
    return rows, problems


# ---------------------------------------------------------------- measurement (child process)
def _io() -> tuple[int, int]:
    try:
        with open("/proc/self/io") as f:
            vals = dict(line.split(": ") for line in f.read().splitlines())
        return int(vals["rchar"]), int(vals["wchar"])
    except OSError:
        return -1, -1


def _hwm_mb() -> float:
    """This process's peak RSS in MB. Linux: VmHWM, which starts fresh at exec; ru_maxrss there is
    carried over from the parent that spawned us, so it can sit above everything this process does.
    Elsewhere: ru_maxrss (bytes on macOS)."""
    import resource

    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / 2**20 if sys.platform == "darwin" else rss / 1024


def _child(case: str, variant: str, workdir: str) -> None:
    import resource

    d = Path(workdir)
    fn, _ = CASES[case]["variants"][variant]
    run = fn(d)  # imports and preparation happen here, outside the measurement
    usage = resource.getrusage(resource.RUSAGE_SELF)
    rss0, cpu0, (r0, w0) = _hwm_mb(), usage.ru_utime + usage.ru_stime, _io()
    t0 = time.perf_counter()
    run()
    wall = time.perf_counter() - t0
    usage = resource.getrusage(resource.RUSAGE_SELF)
    r1, w1 = _io()
    rss1 = _hwm_mb()
    print(
        json.dumps(
            {
                "seconds": wall,
                "cpu_s": usage.ru_utime + usage.ru_stime - cpu0,
                "peak_mb": rss1 - rss0,
                "max_rss_mb": rss1,
                "read_mb": (r1 - r0) / 2**20 if r0 >= 0 else None,
                "written_mb": (w1 - w0) / 2**20 if w0 >= 0 else None,
                "imports_agentl": "agentl" in sys.modules,
                "agentl_file": getattr(sys.modules.get("agentl"), "__file__", "") or "",
            }
        )
    )


def _measure(case: str, variant: str, d: Path, n: int, src: str | None = None) -> dict:
    env = {**os.environ, "BENCH_N": str(n)}
    env.pop("AGENTL_BENCH_SRC", None)
    if src:
        env["AGENTL_BENCH_SRC"] = str(Path(src).resolve())
    for f in ("out.db", "state.db", "out.jsonl", "out.parquet", "out.csv"):
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
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    if src and not out.get("agentl_file", "").startswith(env["AGENTL_BENCH_SRC"]):
        raise RuntimeError(f"{case} / {variant}: agentl was not loaded from {src}")
    return out


BASE = " @ base"  # suffix of a gated agentl variant measured with --against's agentl


def run_all(
    scale: float, repeat: int, only: list[str] | None = None, against: str | None = None
) -> list[dict]:
    """Every case and variant. With `against` (a source tree of another agentl, such as the base
    branch), each gated agentl variant is also measured with that agentl, alternating runs on the
    same machine, as an extra variant named `<variant> @ base`."""
    try:
        import resource  # noqa: F401
    except ImportError as e:
        raise SystemExit("--compare needs the resource module (Linux or macOS)") from e
    out: list[dict[str, Any]] = []
    for case, c in CASES.items():
        if only and case not in only:
            continue
        if not _has(*c["needs"]):
            out.append({"case": case, "skipped": f"needs {', '.join(c['needs'])}"})
            continue
        n = max(10, int(c["rows"] * scale))
        d = Path(tempfile.mkdtemp(prefix="agentl-compare-"))
        if c["make"]:
            c["make"](d, n)
        results, reference = [], None
        gated = {g[1] for g in GATES if g[0] == case} if against else set()
        for variant, (_, needs) in c["variants"].items():
            if not _has(*needs):
                results.append({"variant": variant, "skipped": f"needs {', '.join(needs)}"})
                continue
            print(f"… {case} / {variant}", file=sys.stderr, flush=True)
            # A gated variant alternates this agentl and the base's run by run, so drift on a shared
            # machine hits both alike.
            srcs: list[str | None] = [None, against] if variant in gated else [None]
            runs: dict[str | None, list[dict]] = {src: [] for src in srcs}
            base_error = None
            for _ in range(repeat):
                for src in list(srcs):
                    try:
                        runs[src].append(_measure(case, variant, d, n, src))
                    except RuntimeError as e:
                        if not src:
                            raise
                        # The base's agentl cannot run this job (a feature new in this change):
                        # nothing to compare with, which check_against reports without failing.
                        base_error = str(e).splitlines()[-1][:200]
                        srcs.remove(src)
                        continue
                    data = c["result"](d)
                    if reference is None:
                        reference = data
                    if data != reference:
                        name = variant + (BASE if src else "")
                        raise AssertionError(f"{case}: '{name}' wrote different data from the first variant")
            for src in srcs:
                rs = runs[src]
                best = {
                    k: statistics.median(r[k] for r in rs)
                    for k in ("seconds", "cpu_s", "peak_mb", "max_rss_mb")
                }
                last = rs[-1]
                results.append(
                    {
                        "variant": variant + (BASE if src else ""),
                        **{k: round(v, 3) for k, v in best.items()},
                        "read_mb": None if last["read_mb"] is None else round(last["read_mb"], 1),
                        "written_mb": None if last["written_mb"] is None else round(last["written_mb"], 1),
                        "imports_agentl": last["imports_agentl"],
                    }
                )
            if base_error:
                results.append({"variant": variant + BASE, "skipped": f"the base failed: {base_error}"})
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
    if os.environ.get("AGENTL_BENCH_SRC"):  # --against: import agentl from that source tree
        sys.path.insert(0, os.environ["AGENTL_BENCH_SRC"])
    _child(*sys.argv[1:4])
