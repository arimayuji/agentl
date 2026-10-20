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

EXPECTATIONS = (
    "rows",
    "not_null",
    "unique",
    "accepted",
    "range",
    "fresh",
    "vs_last_run",
    "vs_runs",
    "rejected_rate",
    "pii",
)
# What a failing rule does (#185): `fail` stops the step (or warns, with `on_fail: warn`), `warn`
# only emits an event, `reject` drops the failing rows (row rules only) into the quarantine.
SEVERITIES = ("fail", "warn", "reject")
ROW_RULES = ("not_null", "unique", "accepted", "range")
HISTORY = 50  # passing-run profiles kept per check, for `vs_runs`
_RATE = re.compile(r"^(<=?)?\s*(\d+(?:\.\d+)?)\s*(%?)$")

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


def rejected_limits(rule: Any, default_step: str) -> dict[str, tuple[str, float]]:
    """`rejected_rate: "< 1%"` (the checked step) or `{orders: "< 1%", users: "<= 0.5%"}` ->
    {step: (operator, rate)}. A bare limit ("1%", 0.01) means at most."""
    rules = rule if isinstance(rule, dict) else {default_step: rule}
    out: dict[str, tuple[str, float]] = {}
    for step, limit in rules.items():
        m = _RATE.match(str(limit).strip())
        if not m or isinstance(limit, bool):
            raise ValueError(f"rejected_rate {limit!r}: use a limit like '< 1%', '<= 0.5%' or 0.01")
        rate = float(m.group(2)) / (100 if m.group(3) else 1)
        out[str(step)] = (m.group(1) or "<=", rate)
    return out


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
    records: Records,
    expect: dict[str, Any],
    previous: dict[str, Any] | None,
    now: _dt.datetime | None = None,
    rejected: dict[str, tuple[int, int]] | None = None,
    step: str = "",
    history: list[dict[str, Any]] | None = None,
    protected: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, str]]:
    """Failed expectations as [{check, detail}]. `previous` is the profile of the last passing run;
    `rejected` maps a step to (rejected records, records it saw) in this run; `step` is the
    checked step, for `rejected_rate` given as a single limit; `history` holds the profiles of
    the passing runs, newest first, for `vs_runs`; values of `protected` fields are never shown."""
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
            for cols in unique_keys(rule):
                counts = Counter(k for k in (_key(r, cols) for r in records) if k is not None)
                dups = [(k, c) for k, c in counts.items() if c > 1]
                if dups:
                    extra = sum(c - 1 for _, c in dups)
                    shown = ", ".join(_show_key(cols, k, protected) for k, _ in dups[:3])
                    more = f", {len(dups) - 3} more key(s)" if len(dups) > 3 else ""
                    fail(
                        "unique",
                        f"{'+'.join(cols)}: {extra} duplicate{'s' if extra != 1 else ''} in "
                        f"{len(dups)} key{'s' if len(dups) != 1 else ''} (e.g. {shown}{more})",
                    )
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
            for c, limit in (rule.get("nulls") or {}).items():
                before_rate = _null_rate(previous, c)
                if before_rate is not None:
                    _rate_change(fail, c, before_rate, records, parse_percent(limit))
        elif key == "vs_runs":
            window = (history or [])[: int(rule.get("last", 5))]
            if not window:
                continue  # first run: nothing to compare with
            label = f"median of the last {len(window)} run{'s' if len(window) != 1 else ''}"
            if "rows" in rule:
                before = _median([p.get("rows", 0) for p in window])
                _change(fail, "rows", before, n, parse_percent(rule["rows"]), "vs_runs", label)
            for c, limit in (rule.get("sum") or {}).items():
                sums = [
                    s
                    for s in ((p.get("columns", {}).get(c) or {}).get("sum") for p in window)
                    if s is not None
                ]
                after = sum(x for x in (_number(r.get(c)) for r in records) if x is not None)
                if sums:
                    _change(fail, f"sum of {c}", _median(sums), after, parse_percent(limit), "vs_runs", label)
            for c, limit in (rule.get("nulls") or {}).items():
                rates = [r for r in (_null_rate(p, c) for p in window) if r is not None]
                if rates:
                    _rate_change(fail, c, _median(rates), records, parse_percent(limit), "vs_runs", label)
        elif key == "rejected_rate":
            for name, (op, limit) in rejected_limits(rule, step).items():
                n_bad, total = (rejected or {}).get(name, (0, 0))
                rate = n_bad / total if total else 0.0
                if rate >= limit if op == "<" else rate > limit:
                    fail(
                        "rejected_rate",
                        f"{name}: {n_bad} of {total} records rejected ({rate:.2%}), "
                        f"expected {op} {limit:.2%}",
                    )
        elif key == "pii":
            from .pii import describe, scan

            allow = set() if rule is True else set(_names((rule or {}).get("allow", [])))
            if rule is not False:
                for f in scan(records):
                    if f["field"] not in allow:
                        fail("pii", describe(f))
        else:
            raise ValueError(f"unknown expectation '{key}'. Use {', '.join(EXPECTATIONS)}")
    return fails


def _median(values: list[float]) -> float:
    v = sorted(float(x) for x in values)
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def _change(
    fail: Any,
    label: str,
    before: float,
    after: float,
    limit: float,
    check: str = "vs_last_run",
    against: str = "the last run",
) -> None:
    if before == 0:
        if after != 0:
            fail(check, f"{label}: {after:g} now, 0 on {against}")
        return
    delta = (after - before) / abs(before)
    if abs(delta) > limit:
        fail(check, f"{label}: {after:g} vs {before:g} on {against} ({delta:+.0%}, limit {limit:.0%})")


def _null_rate(prof: dict[str, Any], column: str) -> float | None:
    """Share of empty values of a column in a kept profile; None when the column was not there."""
    col = (prof.get("columns") or {}).get(column)
    rows = prof.get("rows") or 0
    if col is None or not rows:
        return None
    return float(col.get("nulls", 0)) / rows


def _rate_change(
    fail: Any,
    column: str,
    before: float,
    records: Records,
    limit: float,
    check: str = "vs_last_run",
    against: str = "the last run",
) -> None:
    """Null rate drift, in percentage points: rates near zero make a relative change useless."""
    if not records:
        return
    now = sum(1 for r in records if _empty(r.get(column))) / len(records)
    if abs(now - before) > limit:
        fail(
            check,
            f"null rate of {column}: {now:.1%} vs {before:.1%} on {against} "
            f"({(now - before) * 100:+.1f} points, limit {limit * 100:g} points)",
        )


def failing_rows(
    records: Records, key: str, rule: Any, protected: set[str] | frozenset[str] = frozenset()
) -> dict[int, str]:
    """Index -> reason for each record a row rule (`ROW_RULES`) fails, for `severity: reject`.
    For `unique`, the first record with a value is kept and the later ones are rejected."""
    bad: dict[int, str] = {}
    if key == "not_null":
        for c in _names(rule):
            for i, r in enumerate(records):
                if _empty(r.get(c)):
                    bad.setdefault(i, f"not_null: {c} is empty")
    elif key == "unique":
        for cols in unique_keys(rule):
            seen: set[tuple[str, ...]] = set()
            for i, r in enumerate(records):
                k = _key(r, cols)
                if k is None:
                    continue
                if k in seen:
                    bad.setdefault(i, f"unique: duplicate {'+'.join(cols)} {_show_key(cols, k, protected)}")
                seen.add(k)
    elif key == "accepted":
        for c, allowed in rule.items():
            ok = {str(a) for a in allowed}
            for i, r in enumerate(records):
                if not _empty(r.get(c)) and str(r.get(c)) not in ok:
                    bad.setdefault(i, f"accepted: {c} {r.get(c)!r} is not accepted")
    elif key == "range":
        for c, lim in rule.items():
            for i, r in enumerate(records):
                x = _number(r.get(c))
                if x is not None and (("min" in lim and x < lim["min"]) or ("max" in lim and x > lim["max"])):
                    bad.setdefault(i, f"range: {c} {x:g} is out of range")
    else:
        raise ValueError(f"severity reject works only for row rules ({', '.join(ROW_RULES)})")
    return bad


def unique_keys(rule: Any) -> list[tuple[str, ...]]:
    """`unique` -> the keys to check. A string or a flat list checks each column on its own; a
    nested list (`[[date, currency]]`) is one composite key. The two mix: `[id, [date, currency]]`."""
    items = [rule] if isinstance(rule, str) else rule
    if not isinstance(items, list) or not items:
        raise ValueError("unique: use a column, a list of columns, or lists of columns for composite keys")
    out: list[tuple[str, ...]] = []
    for item in items:
        cols = [item] if isinstance(item, str) else item
        if not isinstance(cols, list) or not cols or not all(isinstance(c, str) and c for c in cols):
            raise ValueError(f"unique: {item!r} is not a column name or a non-empty list of column names")
        out.append(tuple(cols))
    return out


def _key(r: dict[str, Any], cols: tuple[str, ...]) -> tuple[str, ...] | None:
    """The key of a record, or None when one of its columns is empty (that is not_null's job)."""
    values = [r.get(c) for c in cols]
    return None if any(_empty(v) for v in values) else tuple(str(v) for v in values)


def _show_key(cols: tuple[str, ...], key: tuple[str, ...], protected: set[str] | frozenset[str]) -> str:
    shown = ["<protected>" if c in protected else repr(v) for c, v in zip(cols, key, strict=True)]
    return shown[0] if len(shown) == 1 else f"({', '.join(shown)})"


def _names(value: str | list[str]) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


# ---------------------------------------------------------------- per-row rules (#218)
def _rule_ok(rule: Any, row: dict[str, Any]) -> bool:
    v = row.get(rule.column)
    if rule.rule == "not_null":
        return not _empty(v)
    if _empty(v):
        return True  # empty values are the job of not_null
    if rule.rule == "between":
        n = _number(v)
        if n is None:
            raise ValueError(f"{rule.column}: {v!r} is not a number")
        return (rule.min is None or n >= rule.min) and (rule.max is None or n <= rule.max)
    if rule.rule == "accepted":
        return str(v) in {str(a) for a in rule.values}
    if rule.rule == "matches":
        return re.search(rule.pattern, str(v)) is not None
    a, b = _number(v), _number(row.get(rule.other))
    if a is None or b is None:
        raise ValueError(f"{rule.column}/{rule.other}: {v!r} and {row.get(rule.other)!r} are not numbers")
    return abs(a - b) <= rule.max


def _identity(rule: Any, row: dict[str, Any], identity: list[str]) -> str:
    cols = identity or [rule.column]
    return "|".join([rule.rule, rule.column, *(f"{c}={row.get(c)}" for c in cols)])


def run_rules(
    records: Records, rules: list[Any], identity: list[str] | None = None
) -> tuple[Records, list[dict[str, Any]], list[str]]:
    """Applies per-row rules. Returns (kept rows, findings, rule errors). A finding is
    {id, rule, column, action, notify, detail}; rows failing a `drop` rule are left out. A rule that
    raises on a row counts as passing for that row and the error is reported, never raised."""
    kept: Records = []
    findings: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, row in enumerate(records):
        drop = False
        for rule in rules:
            try:
                ok = _rule_ok(rule, row)
            except Exception as e:  # a broken rule never breaks the flow
                if len(errors) < 20:
                    errors.append(f"row {i}, rule {rule.rule} on {rule.column}: {e}")
                continue
            if ok:
                continue
            findings.append(
                {
                    "id": _identity(rule, row, identity or []),
                    "rule": rule.rule,
                    "column": rule.column,
                    "action": rule.on_fail,
                    "notify": getattr(rule, "notify", "digest"),
                    "detail": f"row {i}: {rule.column}={row.get(rule.column)!r} failed {rule.rule}",
                }
            )
            drop = drop or rule.on_fail == "drop"
        if not drop:
            kept.append(row)
    return kept, findings, errors


def digest(today: list[str], yesterday: list[str]) -> dict[str, list[str]]:
    """Finding ids compared with the previous day: new, resolved and ongoing (sorted)."""
    t, y = set(today), set(yesterday)
    return {"new": sorted(t - y), "resolved": sorted(y - t), "ongoing": sorted(t & y)}


def digest_ids(findings: list[dict[str, Any]]) -> list[str]:
    """The finding ids that go into the digest: rules with `notify: none` act silently."""
    return sorted({f["id"] for f in findings if f.get("notify", "digest") != "none"})
