"""Run ledger: what one run did (rows per step, rows changed, LLM spend, duration, outcome) and LLM
cost grouped over many runs. Read from the runs and events the store already keeps (#209).

    agentl runs show RUN_ID [--json]
    agentl cost [--since 30d] [--by pipeline,model] [--usd-per-hour X | --lambda-memory-mb MB] [--json]"""

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


LAMBDA_USD_PER_GB_SECOND = 0.0000166667
LAMBDA_USD_PER_REQUEST = 0.0000002
NO_MODEL = "-"


def compute_price(
    usd_per_hour: float | None = None,
    lambda_memory_mb: float | None = None,
    bill_usd: float | None = None,
    usd_per_gb_second: float = LAMBDA_USD_PER_GB_SECOND,
    usd_per_request: float = LAMBDA_USD_PER_REQUEST,
) -> dict[str, Any] | None:
    """The compute pricing for `cost`, from at most one of the three ways to price a host. None
    when none is given (LLM cost only)."""
    given = [
        k
        for k, v in (
            ("usd_per_hour", usd_per_hour),
            ("lambda_memory_mb", lambda_memory_mb),
            ("bill_usd", bill_usd),
        )
        if v is not None
    ]
    if len(given) > 1:
        raise ValueError(f"pick one compute price, got {', '.join(given)}")
    if not given:
        return None
    value = float(
        {"usd_per_hour": usd_per_hour, "lambda_memory_mb": lambda_memory_mb, "bill_usd": bill_usd}[given[0]]
        or 0
    )
    if value < 0:
        raise ValueError(f"{given[0]} must not be negative")
    if given[0] == "usd_per_hour":
        return {"kind": "hourly", "usd_per_hour": value}
    if given[0] == "bill_usd":
        return {"kind": "bill", "bill_usd": value}
    return {
        "kind": "lambda",
        "memory_mb": value,
        "usd_per_gb_second": usd_per_gb_second,
        "usd_per_request": usd_per_request,
    }


def _run_compute(price: dict[str, Any], seconds: dict[str, float]) -> dict[str, float]:
    """Compute cost of each run from its wall seconds:

    - hourly: seconds / 3600 * usd_per_hour (a host, task or pod price);
    - lambda: seconds * memory_mb / 1024 * usd_per_gb_second + usd_per_request;
    - bill: a known bill for a shared host, split over the runs in proportion to wall seconds."""
    if price["kind"] == "hourly":
        return {r: s / 3600 * price["usd_per_hour"] for r, s in seconds.items()}
    if price["kind"] == "lambda":
        gb = price["memory_mb"] / 1024
        return {r: s * gb * price["usd_per_gb_second"] + price["usd_per_request"] for r, s in seconds.items()}
    total = sum(seconds.values())
    if not total:
        return dict.fromkeys(seconds, 0.0)
    return {r: price["bill_usd"] * s / total for r, s in seconds.items()}


def cost(
    store: Any,
    since: float,
    by: list[str],
    now: float | None = None,
    compute: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """LLM calls, tokens and cost of every run started at or after `since`, grouped by `by` (any of
    pipeline, step, model, day). Steps from before per-model spend was recorded count as model "?".

    With `compute` (from `compute_price`), each run's compute cost is attributed too: the run's
    share goes to its steps in proportion to their seconds, and a step's share to its models in
    proportion to their LLM calls (model "-" for steps without LLM calls). A run with no finished
    step keeps its compute under step "-"."""
    import datetime as _dt

    bad = [k for k in by if k not in COST_KEYS]
    if bad or not by:
        raise ValueError(f"--by takes {', '.join(COST_KEYS)}; got {', '.join(bad) or 'nothing'}")
    now = now or time.time()
    runs = store.history(since)
    pipelines = {r["id"]: r["pipeline"] for r in runs}
    groups: dict[tuple, dict] = {}

    def group(run_id: str, step: str, model: str, ts: float) -> dict:
        day = _dt.datetime.fromtimestamp(ts).date().isoformat()
        fields = {"pipeline": pipelines[run_id], "step": step, "model": model, "day": day}
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
                **({"compute_usd": 0.0} if compute else {}),
            },
        )
        g["_runs"].add(run_id)
        return g

    steps: dict[str, list[tuple[dict, float, dict]]] = {}
    for e in store.events_of(since, STEP_END):
        if e["run_id"] not in pipelines:
            continue
        data = e["data"] or {}
        if compute:
            steps.setdefault(e["run_id"], []).append((e, float(data.get("seconds") or 0), data))
        if not data.get("llm_calls"):
            continue
        models = data.get("models") or {
            "?": {k: data.get(k, 0) for k in ("llm_calls", "input_tokens", "output_tokens", "cost_usd")}
        }
        for model, m in models.items():
            g = group(e["run_id"], e["step"], model, e["ts"])
            for k in ("llm_calls", "input_tokens", "output_tokens"):
                g[k] += int(m.get(k) or 0)
            g["cost_usd"] += float(m.get("cost_usd") or 0)

    compute_total = 0.0
    if compute:
        wall = {r["id"]: max(0.0, r["finished"] - r["started"]) for r in runs if r["finished"]}
        for rid, usd in _run_compute(compute, wall).items():
            compute_total += usd
            mine = steps.get(rid) or []
            secs = sum(s for _, s, _ in mine)
            if not mine or not secs:
                run = next(r for r in runs if r["id"] == rid)
                group(rid, NO_MODEL, NO_MODEL, run["started"])["compute_usd"] += usd
                continue
            for e, s, data in mine:
                share = usd * s / secs
                models = data.get("models") or (
                    {"?": {"llm_calls": data["llm_calls"]}} if data.get("llm_calls") else {}
                )
                calls = sum(int(m.get("llm_calls") or 0) for m in models.values())
                if not calls:
                    group(rid, e["step"], NO_MODEL, e["ts"])["compute_usd"] += share
                    continue
                for model, m in models.items():
                    part = share * int(m.get("llm_calls") or 0) / calls
                    group(rid, e["step"], model, e["ts"])["compute_usd"] += part

    rows = []
    for g in groups.values():
        g["runs"] = len(g.pop("_runs"))
        g["cost_usd"] = round(g["cost_usd"], 6)
        if compute:
            g["compute_usd"] = round(g["compute_usd"], 6)
            g["total_usd"] = round(g["cost_usd"] + g["compute_usd"], 6)
        rows.append(g)
    key = "total_usd" if compute else "cost_usd"
    rows.sort(key=lambda g: (-g[key], *(str(g[k]) for k in by)))
    llm = round(sum(g["cost_usd"] for g in rows), 6)
    report: dict[str, Any] = {"since": since, "until": now, "by": by, "total_usd": llm, "groups": rows}
    if compute:
        report["compute"] = compute
        report["llm_usd"] = llm
        report["compute_usd"] = round(compute_total, 6)
        report["total_usd"] = round(llm + compute_total, 6)
    return report


def cost_text(report: dict) -> str:
    if not report["groups"]:
        return "no LLM calls in this period" if "compute" not in report else "no runs in this period"
    lines = []
    for g in report["groups"]:
        label = "  ".join(str(g[k]) for k in report["by"])
        line = (
            f"{label}  {g['llm_calls']} calls in {g['runs']} runs, "
            f"{g['input_tokens']} in / {g['output_tokens']} out tokens, ${g['cost_usd']:.4f}"
        )
        if "compute_usd" in g:
            line += f" + compute ${g['compute_usd']:.4f} = ${g['total_usd']:.4f}"
        lines.append(line)
    if "compute" in report:
        lines.append(
            f"total ${report['total_usd']:.4f} (LLM ${report['llm_usd']:.4f}, "
            f"compute ${report['compute_usd']:.4f}, {report['compute']['kind']})"
        )
    else:
        lines.append(f"total ${report['total_usd']:.4f}")
    return "\n".join(lines)
