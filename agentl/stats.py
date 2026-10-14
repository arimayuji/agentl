"""Track record per pipeline: success rate, durations, cost, failures grouped by reason, and a
per-day trend. Computed from the runs and events the store already keeps; nothing new is stored.

    agentl stats [PIPELINE] [--since 30d] [--json]"""

from __future__ import annotations

import datetime as _dt
import re
import statistics
import time
from typing import Any

FAILURE_EVENTS = ("step.failed", "check.failed", "budget.exceeded")
END_EVENTS = ("run.succeeded", "run.failed")

_NOISE = [
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"(?:[A-Za-z]:)?(?:[\\/][\w.\-]+){2,}"), "<path>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<id>"),
    (re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{6,}\b", re.I), "<id>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
]


def reason(error: str | None) -> str:
    """The error with what changes between runs (numbers, ids, paths, URLs) taken out, so the
    same failure on different days groups together."""
    text = (error or "unknown error").strip().splitlines()[0]
    for pattern, repl in _NOISE:
        text = pattern.sub(repl, text)
    return text[:160]


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[int(q) - 1]


def compute(
    store: Any, since: float, pipeline: str | None = None, now: float | None = None
) -> dict[str, Any]:
    now = now or time.time()
    runs = store.history(since, pipeline)
    ids = {r["id"] for r in runs}
    failing: dict[str, dict] = {}
    cost: dict[str, float] = {}
    for e in store.events_of(since, FAILURE_EVENTS + END_EVENTS):
        if e["run_id"] not in ids:
            continue
        if e["type"] in END_EVENTS:
            cost[e["run_id"]] = float((e["data"] or {}).get("cost_usd") or 0)
        else:
            failing.setdefault(e["run_id"], e)  # the first failure is the cause

    out: dict[str, dict] = {}
    for r in runs:
        p = out.setdefault(
            r["pipeline"],
            {
                "pipeline": r["pipeline"],
                "runs": 0,
                "succeeded": 0,
                "failed": 0,
                "other": 0,
                "_secs": [],
                "cost_usd": 0.0,
                "_reasons": {},
                "_days": {},
            },
        )
        p["runs"] += 1
        status = r["status"] if r["status"] in ("succeeded", "failed") else "other"
        p[status] += 1
        if r["finished"]:
            p["_secs"].append(r["finished"] - r["started"])
        p["cost_usd"] += cost.get(r["id"], 0.0)
        day = _dt.datetime.fromtimestamp(r["started"]).date().isoformat()
        d = p["_days"].setdefault(day, {"date": day, "succeeded": 0, "failed": 0})
        if status != "other":
            d[status] += 1
        p["last_run"], p["last_status"], p["last_started"] = r["id"], r["status"], r["started"]
        if status == "failed":
            ev = failing.get(r["id"])
            step = ev["step"] if ev else None
            key = (step, reason(r["error"]))
            g = p["_reasons"].setdefault(
                key, {"step": step, "reason": key[1], "count": 0, "first_seen": r["started"]}
            )
            g["count"] += 1
            g["last_seen"], g["last_run"], g["last_error"] = r["started"], r["id"], r["error"]

    pipelines = []
    for p in out.values():
        secs = sorted(p.pop("_secs"))
        done = p["succeeded"] + p["failed"]
        p["success_rate"] = round(p["succeeded"] / done, 4) if done else None
        p["median_s"] = round(statistics.median(secs), 3) if secs else None
        p["p95_s"] = round(_pct(secs, 95) or 0, 3) if secs else None
        p["cost_usd"] = round(p["cost_usd"], 6)
        p["failures"] = sorted(p.pop("_reasons").values(), key=lambda g: (-g["count"], -g["last_seen"]))
        p["daily"] = list(p.pop("_days").values())
        pipelines.append(p)
    pipelines.sort(key=lambda p: -p["last_started"])
    return {"since": since, "until": now, "pipelines": pipelines}


def _when(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def text(report: dict, top: int = 5) -> str:
    if not report["pipelines"]:
        return f"no runs since {_when(report['since'])}"
    lines = [f"since {_when(report['since'])}"]
    for p in report["pipelines"]:
        rate = "n/a" if p["success_rate"] is None else f"{p['success_rate']:.0%}"
        lines.append("")
        lines.append(
            f"{p['pipeline']}: {p['runs']} runs, {p['succeeded']} succeeded, {p['failed']} failed"
            f" ({rate} success)"
        )
        if p["median_s"] is not None:
            lines.append(f"  duration: median {p['median_s']:.1f}s, p95 {p['p95_s']:.1f}s")
        if p["cost_usd"]:
            lines.append(f"  cost: {p['cost_usd']:.4f} USD")
        lines.append(f"  last run: {p['last_run']} {p['last_status']}")
        if p["failures"]:
            lines.append("  failures by reason:")
            for g in p["failures"][:top]:
                where = f"[{g['step']}] " if g["step"] else ""
                lines.append(
                    f"    {g['count']:>3}x {where}{g['reason']}"
                    f"  (last {_when(g['last_seen'])}, {g['last_run']})"
                )
        trend = "".join("x" if d["failed"] else "." for d in p["daily"][-30:])
        lines.append(f"  run days, oldest first (. all ok, x some failed): {trend}")
    return "\n".join(lines)
