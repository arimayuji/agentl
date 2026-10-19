"""Track record per pipeline: success rate, durations, cost, failures grouped by reason, a
per-day trend, and per agent step and model what a record, an accepted answer and a cache hit
cost, and the human review queue (#153). Computed from the runs, events and review queue the
store already keeps; nothing new is stored.

    agentl stats [PIPELINE] [--since 30d] [--json]"""

from __future__ import annotations

import datetime as _dt
import re
import statistics
import time
from typing import Any

FAILURE_EVENTS = ("step.failed", "check.failed", "budget.exceeded")
END_EVENTS = ("run.succeeded", "run.failed", "run.skipped")
STEP_EVENTS = ("step.succeeded", "item.rejected")

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
    store: Any,
    since: float,
    pipeline: str | None = None,
    now: float | None = None,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    now = now or time.time()
    runs = store.history(since, pipeline, labels)
    ids = {r["id"] for r in runs}
    failing: dict[str, dict] = {}
    cost: dict[str, float] = {}
    throttled: dict[str, float] = {}  # run id -> seconds waited for llm rate limits
    run_pipeline = {r["id"]: r["pipeline"] for r in runs}
    rejected: dict[tuple[str, str], int] = {}
    done_steps: list[dict] = []
    for e in store.events_of(since, FAILURE_EVENTS + END_EVENTS + STEP_EVENTS):
        if e["run_id"] not in ids:
            continue
        if e["type"] == "item.rejected":
            k = (e["run_id"], e["step"])
            rejected[k] = rejected.get(k, 0) + int((e["data"] or {}).get("count") or 0)
        elif e["type"] == "step.succeeded":
            done_steps.append(e)
        elif e["type"] in END_EVENTS:
            cost[e["run_id"]] = float((e["data"] or {}).get("cost_usd") or 0)
            throttled[e["run_id"]] = float((e["data"] or {}).get("throttled_s") or 0)
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
                "skipped": 0,
                "other": 0,
                "outcomes": {},
                "_secs": [],
                "cost_usd": 0.0,
                "throttled_s": 0.0,
                "_reasons": {},
                "_days": {},
            },
        )
        p["runs"] += 1
        status = r["status"] if r["status"] in ("succeeded", "failed", "skipped") else "other"
        p[status] += 1
        outcome = r.get("outcome") or {"succeeded": "success"}.get(r["status"], r["status"])
        p["outcomes"][outcome] = p["outcomes"].get(outcome, 0) + 1
        if r["finished"]:
            p["_secs"].append(r["finished"] - r["started"])
        p["cost_usd"] += cost.get(r["id"], 0.0)
        p["throttled_s"] += throttled.get(r["id"], 0.0)
        day = _dt.datetime.fromtimestamp(r["started"]).date().isoformat()
        d = p["_days"].setdefault(day, {"date": day, "succeeded": 0, "failed": 0})
        if status in ("succeeded", "failed"):
            d[status] += 1
        p["last_run"], p["last_status"], p["last_started"] = r["id"], r["status"], r["started"]
        p["labels"] = r.get("labels") or {}  # the latest run's labels
        if status == "failed":
            ev = failing.get(r["id"])
            step = ev["step"] if ev else None
            key = (step, reason(r["error"]))
            g = p["_reasons"].setdefault(
                key, {"step": step, "reason": key[1], "count": 0, "first_seen": r["started"]}
            )
            g["count"] += 1
            g["last_seen"], g["last_run"], g["last_error"] = r["started"], r["id"], r["error"]

    agents = _agent_costs(done_steps, rejected, run_pipeline)
    queue: dict[str, dict[str, int]] = {}
    if hasattr(store, "review_counts"):
        for c in store.review_counts(pipeline):
            q = queue.setdefault(c["pipeline"], {"pending": 0, "reviewed": 0})
            q["pending" if c["status"] == "pending" else "reviewed"] += int(c["count"])
    pipelines = []
    for p in out.values():
        p["agents"] = agents.get(p["pipeline"], [])
        p["review"] = queue.get(p["pipeline"], {"pending": 0, "reviewed": 0})
        secs = sorted(p.pop("_secs"))
        done = p["succeeded"] + p["failed"]
        p["success_rate"] = round(p["succeeded"] / done, 4) if done else None
        p["median_s"] = round(statistics.median(secs), 3) if secs else None
        p["p95_s"] = round(_pct(secs, 95) or 0, 3) if secs else None
        p["cost_usd"] = round(p["cost_usd"], 6)
        p["throttled_s"] = round(p["throttled_s"], 3)
        p["failures"] = sorted(p.pop("_reasons").values(), key=lambda g: (-g["count"], -g["last_seen"]))
        p["daily"] = list(p.pop("_days").values())
        pipelines.append(p)
    pipelines.sort(key=lambda p: -p["last_started"])
    return {"since": since, "until": now, "pipelines": pipelines}


def _per(cost: float, n: int) -> float | None:
    return round(cost / n, 6) if n else None


def _agent_costs(
    done: list[dict], rejected: dict[tuple[str, str], int], run_pipeline: dict[str, str]
) -> dict[str, list[dict]]:
    """Per pipeline, one row per agent step and model: what a record, an accepted answer and a
    cache hit cost. Records are the step's output plus the ones it rejected; accepted answers
    are the ones not rejected. A cache hit avoided is priced at what an answered record cost."""
    rows: dict[tuple[str, str, str], dict] = {}
    for e in done:
        d = e["data"] or {}
        if "llm_calls" not in d:  # not an agent step
            continue
        key = (run_pipeline[e["run_id"]], e["step"], str(d.get("model") or "unknown"))
        row = rows.setdefault(
            key,
            {
                "step": key[1],
                "model": key[2],
                "runs": 0,
                "records": 0,
                "accepted": 0,
                "rejected": 0,
                "answered": 0,
                "cache_hits": 0,
                "cost_usd": 0.0,
                "review_queued": 0,
                "review_held": 0,
                "review_reused": 0,
            },
        )
        bad = rejected.get((e["run_id"], e["step"]), 0)
        accepted = int(d.get("records") or 0)
        row["runs"] += 1
        row["accepted"] += accepted
        row["rejected"] += bad
        row["records"] += accepted + bad
        row["answered"] += sum((d.get("answered_by") or {}).values())
        row["cache_hits"] += int(d.get("cache_hits") or 0)
        row["cost_usd"] += float(d.get("cost_usd") or 0)
        rv = d.get("review") or {}
        row["review_queued"] += int(rv.get("queued") or 0)
        row["review_held"] += int(rv.get("held") or 0)
        row["review_reused"] += int(rv.get("reused") or 0) + int(rv.get("dropped") or 0)
    out: dict[str, list[dict]] = {}
    for (pipeline, _, _), row in sorted(rows.items()):
        c = row["cost_usd"]
        row["cost_usd"] = round(c, 6)
        row["cost_per_record"] = _per(c, row["records"])
        row["cost_per_accepted"] = _per(c, row["accepted"])
        per_answer = _per(c, row["answered"])
        row["cost_per_answer"] = per_answer
        row["cache_saved_usd"] = round(per_answer * row["cache_hits"], 6) if per_answer else None
        out.setdefault(pipeline, []).append(row)
    return out


def _usd(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.6f}"


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
        named = {k: v for k, v in p["outcomes"].items() if k not in ("success", "failed")}
        if named or p["skipped"]:
            lines.append("  outcomes: " + ", ".join(f"{k} {v}" for k, v in sorted(p["outcomes"].items())))
        if p["median_s"] is not None:
            lines.append(f"  duration: median {p['median_s']:.1f}s, p95 {p['p95_s']:.1f}s")
        if p["cost_usd"]:
            lines.append(f"  cost: {p['cost_usd']:.4f} USD")
        if p.get("throttled_s"):
            lines.append(f"  throttled: {p['throttled_s']:.1f} s waiting for llm rate limits")
        lines.append(f"  last run: {p['last_run']} {p['last_status']}")
        if p.get("agents"):
            lines.append("  agent cost (USD):")
            for a in p["agents"]:
                saved = f", cache saved {a['cache_saved_usd']:.4f}" if a["cache_saved_usd"] else ""
                lines.append(
                    f"    [{a['step']}] {a['model']}: {_usd(a['cost_per_record'])}/record,"
                    f" {_usd(a['cost_per_accepted'])}/accepted ({a['accepted']} of {a['records']}),"
                    f" {a['cache_hits']} cache hits{saved}"
                )
        q = p.get("review") or {}
        if q.get("pending") or q.get("reviewed"):
            lines.append(f"  review queue: {q['pending']} pending, {q['reviewed']} reviewed")
        for a in p.get("agents") or []:
            if a.get("review_queued") or a.get("review_held") or a.get("review_reused"):
                lines.append(
                    f"    [{a['step']}] review: {a['review_queued']} queued, {a['review_held']} held,"
                    f" {a['review_reused']} reused from people"
                )
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
