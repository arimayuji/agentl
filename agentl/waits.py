"""Conditions for `wait_until` steps.

rows > 0                 how many records the source returned
max(date) == today       an aggregate of a field: max, min, sum or count, against a number,
min(price) >= 1.5        a quoted string, `today` or `yesterday` (compared as YYYY-MM-DD)
changed                  the records differ from the last run where the wait succeeded
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import operator
import re
from collections.abc import Callable
from typing import Any

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}
_ROWS = re.compile(r"^rows\s*(>=|<=|==|!=|>|<)\s*(\d+)$")
_AGG = re.compile(r"^(max|min|sum|count)\((\w+)\)\s*(>=|<=|==|!=|>|<)\s*(.+)$")


def parse_condition(text: str) -> tuple:
    t = text.strip()
    if t == "changed":
        return ("changed",)
    if m := _ROWS.match(t):
        return ("rows", m.group(1), int(m.group(2)))
    if m := _AGG.match(t):
        return ("agg", m.group(1), m.group(2), m.group(3), m.group(4).strip())
    raise ValueError(
        f"wait_until condition {text!r}: use 'rows > 0', 'max(date) == today' "
        "(max, min, sum or count of a field) or 'changed'"
    )


def fingerprint(records: list[dict]) -> str:
    return hashlib.sha256(json.dumps(records, sort_keys=True, default=str).encode()).hexdigest()


def _value(raw: str, today: _dt.date) -> tuple[Any, bool]:
    """The right-hand side and whether it is a date (compared on the first 10 characters)."""
    if raw == "today":
        return today.isoformat(), True
    if raw == "yesterday":
        return (today - _dt.timedelta(days=1)).isoformat(), True
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1], False
    try:
        return float(raw), False
    except ValueError as e:
        raise ValueError(f"wait_until: cannot read {raw!r}; quote strings, e.g. 'done'") from e


def holds(
    condition: str, records: list[dict], last: str | None = None, today: _dt.date | None = None
) -> bool:
    """Whether `condition` holds for `records`. `last` is the fingerprint of the last good run."""
    c = parse_condition(condition)
    today = today or _dt.date.today()
    if c[0] == "changed":
        return bool(records) and fingerprint(records) != last
    if c[0] == "rows":
        return _OPS[c[1]](len(records), c[2])
    _, agg, field, op, raw = c
    right, is_date = _value(raw, today)
    values = [r.get(field) for r in records if r.get(field) not in (None, "")]
    if agg == "count":
        return _OPS[op](len(values), right)
    if not values:
        return False
    if is_date or isinstance(right, str):
        texts = [str(v)[:10] if is_date else str(v) for v in values]
        left: Any = max(texts) if agg == "max" else min(texts) if agg == "min" else None
        if left is None:
            raise ValueError(f"wait_until: sum({field}) needs a number on the right")
        return _OPS[op](left, right)
    nums = []
    for v in values:
        try:
            nums.append(float(str(v)))
        except (TypeError, ValueError):
            continue
    if not nums:
        return False
    left = max(nums) if agg == "max" else min(nums) if agg == "min" else sum(nums)
    return _OPS[op](left, right)
