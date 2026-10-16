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
    p.add_argument("--only", nargs="*", help="run only these scenarios (or, with --compare, these cases)")
    p.add_argument("--compare", action="store_true", help="the same jobs with and without agentl")
    p.add_argument(
        "--check",
        metavar="BASELINE",
        help="with --compare: fail (exit 1) when agentl got slower or heavier than in BASELINE",
    )
    p.add_argument(
        "--write-baseline", metavar="PATH", help="with --compare: write the ratios the gate compares to PATH"
    )
    p.add_argument(
        "--against",
        metavar="SRC",
        help="with --compare: also run the gated agentl variants with the agentl in source tree SRC "
        "(the base branch, say), alternating on this machine, and fail (exit 1) on a regression",
    )
    p.add_argument(
        "--repeat", type=int, help="with --compare: runs per variant (median); default 3, 1 with --quick"
    )
    a = p.parse_args()

    import agentl

    if a.compare:
        return _compare(a)

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


def _compare(a: argparse.Namespace) -> int:
    import agentl

    from . import compare

    repeat = a.repeat or (1 if a.quick else 3)
    results = compare.run_all(0.1 if a.quick else 1.0, repeat=repeat, only=a.only, against=a.against)
    py, arch, cpus = platform.python_version(), platform.machine(), os.cpu_count()
    print(f"\nagentl {agentl.__version__} · Python {py} · {arch} · {cpus} CPUs")
    print(compare.table(results))
    if a.json:
        meta = {
            "agentl": agentl.__version__,
            "python": py,
            "machine": arch,
            "cpus": cpus,
            "date": time.strftime("%Y-%m-%d"),
            "quick": a.quick,
        }
        with open(a.json, "w") as f:
            json.dump({"meta": meta, "compare": results}, f, indent=2)
    if a.write_baseline:
        base = {
            "agentl": agentl.__version__,
            "quick": a.quick,
            "repeat": repeat,
            "ratios": compare.ratios(results),
        }
        with open(a.write_baseline, "w") as f:
            json.dump(base, f, indent=2)
            f.write("\n")
        print(f"\nbaseline written to {a.write_baseline}")
    if a.against:
        rows, problems = compare.check_against(results)
        print(f"\nregression gate against the agentl in {a.against}, same machine:")
        for case, r in rows.items():
            if "note" in r:
                print(f"  {case}: {r['note']}")
                continue
            print(
                f"  {case}: {r['time_ratio']:.2f}x the base's time, "
                f"peak RSS {r['rss_mb']:.0f} MB (base {r['base_rss_mb']:.0f})"
            )
        if problems:
            print("FAILED:\n  " + "\n  ".join(problems))
            return 1
        print("passed")
    if a.check:
        with open(a.check) as f:
            baseline = json.load(f)
        problems = compare.check(results, baseline)
        print("\nregression gate against " + a.check + f" (agentl {baseline.get('agentl')}):")
        for case, r in compare.ratios(results).items():
            b = baseline.get("ratios", {}).get(case, {})
            print(
                f"  {case}: {r['time_ratio']:.2f}x the reference "
                f"(baseline {b.get('time_ratio', 'n/a')}x), {r['peak_mb']:.0f} MB"
            )
        if problems:
            print("FAILED:\n  " + "\n  ".join(problems))
            return 1
        print("passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
