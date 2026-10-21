"""Answer drift of agent steps (#155): a profile of what an agent step answered in a run, compared
with the mean of its last runs.

A step with `drift: {window: 7, threshold: 0.15}` profiles its answers after each successful run:
the reject rate, the review rate (#153), the share of each value of its `str` and `bool` output
fields, the mean of its `int` and `float` fields and the cost per record. The profile is compared
with the mean of the last `window` profiles; a change beyond `threshold` emits `step.drift` with the
before and after values. Rates and shares change by absolute points (0.15 is 15 points); means and
the cost per record by relative change (0.15 is 15%). The profile travels on `step.succeeded`, so
`agentl stats` repeats the comparison from the run store."""

from __future__ import annotations

from typing import Any

MAX_VALUES = 20  # a str field with more distinct values is free text: no distribution
OTHER = "(other)"


def profile(
    records: list[dict],
    output: dict[str, str],
    rejected: int,
    review_queued: int,
    cost_usd: float,
) -> dict[str, Any]:
    """The profile of one run of an agent step: `records` are its accepted answers, `output` its
    output schema. Values are never kept for numeric fields, only means."""
    total = len(records) + rejected
    rates: dict[str, float] = {}
    if total:
        rates["reject_rate"] = round(rejected / total, 4)
        rates["review_rate"] = round(review_queued / total, 4)
    means: dict[str, float] = {}
    shares: dict[str, dict[str, float]] = {}
    for name, type_name in output.items():
        base = type_name.rstrip("?")
        values = [r.get(name) for r in records if r.get(name) is not None]
        if not values:
            continue
        if base in ("int", "float"):
            nums = [float(v) for v in values if isinstance(v, int | float) and not isinstance(v, bool)]
            if nums:
                means[name] = round(sum(nums) / len(nums), 6)
        elif base in ("str", "bool"):
            counts: dict[str, int] = {}
            for v in values:
                key = str(v).lower() if isinstance(v, bool) else str(v)
                counts[key] = counts.get(key, 0) + 1
            if len(counts) <= MAX_VALUES:
                shares[name] = {k: round(n / len(values), 4) for k, n in sorted(counts.items())}
    out: dict[str, Any] = {"records": total, **rates, "means": means, "shares": shares}
    if total:
        out["cost_per_record"] = round(cost_usd / total, 8)
    return out


def _flat(p: dict[str, Any]) -> dict[str, tuple[str, float]]:
    """metric name -> (kind, value); kind `rate` compares points, `mean` relative change."""
    out: dict[str, tuple[str, float]] = {}
    for k in ("reject_rate", "review_rate"):
        if k in p:
            out[k] = ("rate", float(p[k]))
    if "cost_per_record" in p:
        out["cost_per_record"] = ("mean", float(p["cost_per_record"]))
    for f, v in (p.get("means") or {}).items():
        out[f"mean({f})"] = ("mean", float(v))
    for f, dist in (p.get("shares") or {}).items():
        for value, share in dist.items():
            out[f"share({f}={value})"] = ("rate", float(share))
    return out


def baseline(history: list[dict[str, Any]]) -> dict[str, tuple[str, float]]:
    """The mean of each metric over `history`. A value missing from a run's distribution counts as
    a share of 0 in that run; other metrics average over the runs that have them."""
    flats = [_flat(h) for h in history]
    out: dict[str, tuple[str, float]] = {}
    for name in {n for f in flats for n in f}:
        kind = next(f[name][0] for f in flats if name in f)
        if kind == "rate" and name.startswith("share("):
            field = name[6 : name.index("=")]
            vals = [
                f[name][1] if name in f else 0.0
                for f, h in zip(flats, history, strict=True)
                if field in (h.get("shares") or {})
            ]
        else:
            vals = [f[name][1] for f in flats if name in f]
        if vals:
            out[name] = (kind, sum(vals) / len(vals))
    return out


def compare(now: dict[str, Any], history: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    """The metrics of `now` that moved beyond `threshold` from the mean of `history`, as
    {metric, before, after, change}; empty without history. A share that drops to nothing (a value
    the step stopped answering) counts as 0 now."""
    if not history:
        return []
    before = baseline(history)
    after = _flat(now)
    fields = set((now.get("shares") or {}).keys())
    changes: list[dict[str, Any]] = []
    for name in sorted(set(before) | set(after)):
        if name not in after:
            if not (name.startswith("share(") and name[6 : name.index("=")] in fields):
                continue
            after_v = 0.0
            kind = before[name][0]
        else:
            kind, after_v = after[name]
        if name not in before:
            if not (name.startswith("share(") and name[6 : name.index("=")] in _fields(history)):
                continue
            before_v = 0.0
        else:
            before_v = before[name][1]
        if kind == "rate":
            change = after_v - before_v
            moved = abs(change) > threshold
        else:
            if before_v == 0:
                moved, change = after_v != 0, (1.0 if after_v else 0.0)
            else:
                change = (after_v - before_v) / abs(before_v)
                moved = abs(change) > threshold
        if moved:
            changes.append(
                {
                    "metric": name,
                    "kind": "points" if kind == "rate" else "relative",
                    "before": round(before_v, 6),
                    "after": round(after_v, 6),
                    "change": round(change, 4),
                }
            )
    return changes


def _fields(history: list[dict[str, Any]]) -> set[str]:
    return {f for h in history for f in (h.get("shares") or {})}


def describe(change: dict[str, Any]) -> str:
    """One line for a change, for the CLI and notifications."""
    if change["kind"] == "points":
        before, after = f"{change['before']:.0%}", f"{change['after']:.0%}"
        return f"{change['metric']}: {before} -> {after} ({change['change']:+.0%} pts)"
    return f"{change['metric']}: {change['before']:g} -> {change['after']:g} ({change['change']:+.0%})"
