"""Data checks: fixed expectations, a profile of the data, and the comparison with the last run.

A `check` step runs these on its input and passes the records through unchanged, so it sits
between a transform and a writer. The optional agent review (in the runner) reads the profile,
not the whole data."""

from __future__ import annotations

import datetime as _dt
import math
import re
from collections import Counter
from typing import Any

Records = list[dict[str, Any]]

EXPECTATIONS = ("rows", "not_null", "unique", "accepted", "range", "fresh", "vs_last_run")

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhdw])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(value: str | int | float) -> float:
    """'30m', '6h', '1d', '2w' or seconds -> seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    m = _DURATION.match(str(value).strip())
    if not m:
        raise ValueError(f"duration '{value}': use a number with s, m, h, d or w (e.g. 6h, 1d)")
    return float(m.group(1)) * _UNITS[m.group(2)]


def parse_percent(value: str | int | float) -> float:
    """'30%' or 0.3 -> 0.3."""
    if isinstance(value, str) and value.strip().endswith("%"):
        return float(value.strip()[:-1]) / 100
    return float(value)


def _number(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return None if isinstance(v, float) and math.isnan(v) else float(v)
    try:
        return float(str(v).replace(",", "")) if str(v).strip() else None
    except ValueError:
        return None


def _when(v: Any) -> _dt.datetime | None:
    if isinstance(v, _dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=_dt.timezone.utc)
    if isinstance(v, _dt.date):
        return _dt.datetime(v.year, v.month, v.day, tzinfo=_dt.timezone.utc)
    if isinstance(v, str) and v.strip():
        try:
            d = _dt.datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)
    return None


def _empty(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, float) and math.isnan(v))


# ---------------------------------------------------------------- profile
def profile(records: Records, sample: int = 15) -> dict[str, Any]:
    """Per-column stats an LLM (or a person) can read: counts, nulls, distinct values, numeric
    min/max/mean/sum, the most common values, plus a small sample of rows."""
    cols: list[str] = []
    for r in records:
        cols.extend(k for k in r if k not in cols)
    out: dict[str, Any] = {"rows": len(records), "columns": {}}
    for c in cols:
        values = [r.get(c) for r in records]
        present = [v for v in values if not _empty(v)]
        nums = [n for n in (_number(v) for v in present) if n is not None]
        col: dict[str, Any] = {"nulls": len(values) - len(present)}
        distinct = Counter(str(v) for v in present)
        col["distinct"] = len(distinct)
        if nums and len(nums) == len(present):
            col.update(
                min=min(nums), max=max(nums), mean=round(sum(nums) / len(nums), 4), sum=round(sum(nums), 4)
            )
        else:
            col["top"] = [[v, n] for v, n in distinct.most_common(5)]
        out["columns"][c] = col
    step = max(1, len(records) // sample) if records else 1
    out["sample"] = [records[i] for i in range(0, len(records), step)][:sample]
    return out


# ---------------------------------------------------------------- expectations
def run_expectations(
    records: Records, expect: dict[str, Any], previous: dict[str, Any] | None, now: _dt.datetime | None = None
) -> list[dict[str, str]]:
    """Failed expectations as [{check, detail}]. `previous` is the profile of the last passing run."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    fails: list[dict[str, str]] = []

    def fail(name: str, detail: str) -> None:
        fails.append({"check": name, "detail": detail})

    n = len(records)
    for key, rule in expect.items():
        if key == "rows":
            if "min" in rule and n < rule["min"]:
                fail("rows", f"{n} rows, expected at least {rule['min']}")
            if "max" in rule and n > rule["max"]:
                fail("rows", f"{n} rows, expected at most {rule['max']}")
        elif key == "not_null":
            for c in _names(rule):
                nulls = sum(1 for r in records if _empty(r.get(c)))
                if nulls:
                    fail("not_null", f"{c}: {nulls} empty value{'s' if nulls != 1 else ''}")
        elif key == "unique":
            for c in _names(rule):
                counts = Counter(str(r.get(c)) for r in records if not _empty(r.get(c)))
                dup = sum(k - 1 for k in counts.values() if k > 1)
                if dup:
                    example = next(v for v, k in counts.items() if k > 1)
                    fail("unique", f"{c}: {dup} duplicate{'s' if dup != 1 else ''} (e.g. {example!r})")
        elif key == "accepted":
            for c, allowed in rule.items():
                ok = {str(a) for a in allowed}
                bad = Counter(
                    str(r.get(c)) for r in records if not _empty(r.get(c)) and str(r.get(c)) not in ok
                )
                if bad:
                    shown = ", ".join(repr(v) for v, _ in bad.most_common(3))
                    fail("accepted", f"{c}: {sum(bad.values())} unexpected value(s): {shown}")
        elif key == "range":
            for c, lim in rule.items():
                nums = [x for x in (_number(r.get(c)) for r in records) if x is not None]
                low = [x for x in nums if "min" in lim and x < lim["min"]]
                high = [x for x in nums if "max" in lim and x > lim["max"]]
                if low:
                    fail("range", f"{c}: {len(low)} value(s) below {lim['min']} (lowest {min(low)})")
                if high:
                    fail("range", f"{c}: {len(high)} value(s) above {lim['max']} (highest {max(high)})")
        elif key == "fresh":
            field, max_age = rule["field"], parse_duration(rule["max_age"])
            dates = [d for d in (_when(r.get(field)) for r in records) if d is not None]
            if not dates:
                fail("fresh", f"{field}: no dates found")
            else:
                age = (now - max(dates)).total_seconds()
                if age > max_age:
                    fail(
                        "fresh", f"{field}: newest is {max(dates).isoformat()}, older than {rule['max_age']}"
                    )
        elif key == "vs_last_run":
            if previous is None:
                continue  # first run: nothing to compare with
            if "rows" in rule:
                _change(fail, "rows", previous.get("rows", 0), n, parse_percent(rule["rows"]))
            for c, limit in (rule.get("sum") or {}).items():
                before = (previous.get("columns", {}).get(c) or {}).get("sum")
                after = sum(x for x in (_number(r.get(c)) for r in records) if x is not None)
                if before is not None:
                    _change(fail, f"sum of {c}", before, after, parse_percent(limit))
        else:
            raise ValueError(f"unknown expectation '{key}'. Use {', '.join(EXPECTATIONS)}")
    return fails


def _change(fail: Any, label: str, before: float, after: float, limit: float) -> None:
    if before == 0:
        if after != 0:
            fail("vs_last_run", f"{label}: {after:g} now, 0 on the last run")
        return
    delta = (after - before) / abs(before)
    if abs(delta) > limit:
        fail(
            "vs_last_run",
            f"{label}: {after:g} vs {before:g} on the last run ({delta:+.0%}, limit {limit:.0%})",
        )


def _names(value: str | list[str]) -> list[str]:
    return [value] if isinstance(value, str) else list(value)
