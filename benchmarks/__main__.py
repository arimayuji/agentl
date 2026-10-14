"""Run every scenario and print a table (and optionally JSON).

Everything runs offline: local files, a local HTTP server, a fake LLM with fixed latency, SQLite
and, when AGENTL_TEST_PG_DSN is set, Postgres. Numbers depend on the machine; compare runs made on
the same machine (the JSON records it)."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time

from . import scenarios


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m benchmarks")
    p.add_argument("--quick", action="store_true", help="smaller inputs, for CI")
    p.add_argument("--json", metavar="PATH", help="also write the results as JSON")
    p.add_argument("--only", nargs="*", help="run only these scenarios")
    a = p.parse_args()

    import agentl

    scale = 0.1 if a.quick else 1.0
    results = []
    for name, fn in scenarios.ALL:
        if a.only and name not in a.only:
            continue
        print(f"… {name}", file=sys.stderr, flush=True)
        try:
            results.append({"scenario": name, **fn(scale)})
        except scenarios.Skip as e:
            results.append({"scenario": name, "skipped": str(e)})

    if not results:
        names = ", ".join(name for name, _ in scenarios.ALL)
        print(f"no scenario matched; available: {names}", file=sys.stderr)
        return 2
    width = max(len(r["scenario"]) for r in results)
    py, arch, cpus = platform.python_version(), platform.machine(), os.cpu_count()
    print(f"\nagentl {agentl.__version__} · Python {py} · {arch} · {cpus} CPUs\n")
    for r in results:
        if "skipped" in r:
            print(f"{r['scenario']:<{width}}  skipped: {r['skipped']}")
            continue
        extra = "  ".join(f"{k}={v}" for k, v in r.items() if k not in ("scenario", "summary"))
        print(f"{r['scenario']:<{width}}  {r['summary']:<34}  {extra}")
    if a.json:
        meta = {
            "agentl": agentl.__version__,
            "python": platform.python_version(),
            "machine": platform.machine(),
            "cpus": os.cpu_count(),
            "date": time.strftime("%Y-%m-%d"),
            "quick": a.quick,
        }
        with open(a.json, "w") as f:
            json.dump({"meta": meta, "results": results}, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
