"""Run ledger: what one run did (rows per step, rows changed, LLM spend, duration, outcome) and LLM
cost grouped over many runs. Read from the runs and events the store already keeps (#209).

    agentl runs show RUN_ID [--json]
    agentl cost [--since 30d] [--by pipeline,model] [--json]"""

from __future__ import annotations

import time
from typing import Any

STEP_END = ("step.succeeded", "step.failed")
CHANGED = ("inserted", "updated")
COST_KEYS = ("pipeline", "step", "model", "day")


def show(store: Any, run_id: str) -> dict[str, Any] | None:
    """The ledger of one run, or None when the store has no such run."""
    run = store.run(run_id)
    if run is None:
        return None
    steps: dict[str, dict] = {}
    end: dict[str, Any] = {}
    for e in store.events(run_id):
        data = e["data"] or {}
        if e["type"] == "step.started" and e["step"]:
            steps.setdefault(e["step"], {"step": e["step"], "kind": data.get("kind"), "status": "running"})
        elif e["type"] in STEP_END and e["step"]:
            s = steps.setdefault(e["step"], {"step": e["step"], "kind": None})
            s["status"] = e["type"].split(".", 1)[1]
            for k in ("records", "written", "inserted", "updated", "unchanged", "seconds", "error"):
                if k in data:
                    s[k] = data[k]
            for k in ("llm_calls", "input_tokens", "output_tokens", "cost_usd", "models"):
                if data.get(k):
                    s[k] = data[k]
        elif e["type"] in ("run.succeeded", "run.failed"):
            end = data
    rows = list(steps.values())
    seconds = end.get("seconds")
    if seconds is None and run["finished"]:
        seconds = round(run["finished"] - run["started"], 3)
    return {
        "id": run["id"],
        "pipeline": run["pipeline"],
        "status": run["status"],
        "started": run["started"],
        "finished": run["finished"],
        "seconds": seconds,
        "error": run["error"],
        "changed": sum(s.get(k, 0) for s in rows for k in CHANGED),
        "llm_calls": sum(s.get("llm_calls", 0) for s in rows),
        "input_tokens": end.get("input_tokens", sum(s.get("input_tokens", 0) for s in rows)),
        "output_tokens": end.get("output_tokens", sum(s.get("output_tokens", 0) for s in rows)),
        "cost_usd": end.get("cost_usd", round(sum(s.get("cost_usd", 0.0) for s in rows), 6)),
        "rejected": end.get("rejected", 0),
        "steps": rows,
    }


def show_text(ledger: dict) -> str:
    secs = "?" if ledger["seconds"] is None else f"{ledger['seconds']}s"
    lines = [
        f"run {ledger['id']}  {ledger['pipeline']}  {ledger['status']}  {secs}",
        f"changed {ledger['changed']} rows, {ledger['rejected']} rejected; {ledger['llm_calls']} LLM calls, "
        f"{ledger['input_tokens']} in / {ledger['output_tokens']} out tokens, ${ledger['cost_usd']:.4f}",
    ]
    if ledger["error"]:
        lines.append(f"error: {ledger['error']}")
    for s in ledger["steps"]:
        parts = [f"{s.get('records', '-')} records"]
        parts += [f"{s[k]} {k}" for k in ("written", "inserted", "updated", "unchanged") if k in s]
        if s.get("llm_calls"):
            models = ", ".join(s.get("models") or {})
            parts.append(
                f"{s['llm_calls']} calls ${s.get('cost_usd', 0):.4f}" + (f" ({models})" if models else "")
            )
        if "seconds" in s:
            parts.append(f"{s['seconds']}s")
        lines.append(f"  {s['step']:<20} {s['status']:<10} " + ", ".join(parts))
        if s.get("error"):
            lines.append(f"    {s['error']}")
    return "\n".join(lines)


def cost(store: Any, since: float, by: list[str], now: float | None = None) -> dict[str, Any]:
    """LLM calls, tokens and cost of every run started at or after `since`, grouped by `by` (any of
    pipeline, step, model, day). Steps from before per-model spend was recorded count as model "?"."""
    import datetime as _dt

    bad = [k for k in by if k not in COST_KEYS]
    if bad or not by:
        raise ValueError(f"--by takes {', '.join(COST_KEYS)}; got {', '.join(bad) or 'nothing'}")
    now = now or time.time()
    pipelines = {r["id"]: r["pipeline"] for r in store.history(since)}
    groups: dict[tuple, dict] = {}
    for e in store.events_of(since, STEP_END):
        if e["run_id"] not in pipelines:
            continue
        data = e["data"] or {}
        if not data.get("llm_calls"):
            continue
        models = data.get("models") or {
            "?": {k: data.get(k, 0) for k in ("llm_calls", "input_tokens", "output_tokens", "cost_usd")}
        }
        day = _dt.datetime.fromtimestamp(e["ts"]).date().isoformat()
        for model, m in models.items():
            fields = {"pipeline": pipelines[e["run_id"]], "step": e["step"], "model": model, "day": day}
            key = tuple(fields[k] for k in by)
            g = groups.setdefault(
                key,
                {
                    **{k: fields[k] for k in by},
                    "_runs": set(),
                    "llm_calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0.0,
                },
            )
            g["_runs"].add(e["run_id"])
            for k in ("llm_calls", "input_tokens", "output_tokens"):
                g[k] += int(m.get(k) or 0)
            g["cost_usd"] += float(m.get("cost_usd") or 0)
    rows = []
    for g in groups.values():
        g["runs"] = len(g.pop("_runs"))
        g["cost_usd"] = round(g["cost_usd"], 6)
        rows.append(g)
    rows.sort(key=lambda g: (-g["cost_usd"], *(str(g[k]) for k in by)))
    total = round(sum(g["cost_usd"] for g in rows), 6)
    return {"since": since, "until": now, "by": by, "total_usd": total, "groups": rows}


def cost_text(report: dict) -> str:
    if not report["groups"]:
        return "no LLM calls in this period"
    lines = []
    for g in report["groups"]:
        label = "  ".join(str(g[k]) for k in report["by"])
        lines.append(
            f"{label}  {g['llm_calls']} calls in {g['runs']} runs, "
            f"{g['input_tokens']} in / {g['output_tokens']} out tokens, ${g['cost_usd']:.4f}"
        )
    lines.append(f"total ${report['total_usd']:.4f}")
    return "\n".join(lines)
