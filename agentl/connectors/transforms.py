"""Deterministic transforms that don't need an LLM."""

from __future__ import annotations

from typing import Any

from ..registry import connector, rowwise
from ._util import single


def _names(value: str | list[str] | None) -> list[str]:
    """Accept `on: id` as well as `on: [id, date]`."""
    return [value] if isinstance(value, str) else list(value or [])


@connector("select")
def select(
    input: Any, fields: str | list[str] | None = None, rename: dict[str, str] | None = None
) -> list[dict]:
    out = []
    for r in single(input):
        row = {k: r.get(k) for k in _names(fields)} if fields else dict(r)
        for old, new in (rename or {}).items():
            if old in row:
                row[new] = row.pop(old)
        out.append(row)
    return out


@connector("filter")
def filter_(
    input: Any, where: dict[str, Any] | None = None, not_empty: str | list[str] | None = None
) -> list[dict]:
    rows = single(input)
    where = where or {}
    return [
        r
        for r in rows
        if all(r.get(k) == v for k, v in where.items())
        and all(r.get(k) not in (None, "") for k in _names(not_empty))
    ]


rowwise("select")
rowwise("filter")


@connector("dedupe")
def dedupe(input: Any, on: str | list[str]) -> list[dict]:
    """Keep the first record for each `on` key, in input order."""
    on = _names(on)
    seen, out = set(), []
    for r in single(input):
        key = tuple(r.get(k) for k in on)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


@connector("join")
def join(input: Any, left: str, right: str, on: str, how: str = "inner") -> list[dict]:
    if not isinstance(input, dict):
        raise ValueError("join needs two inputs: input: [left_step, right_step]")
    index: dict[Any, list[dict]] = {}
    for r in input[right]:
        index.setdefault(str(r.get(on)), []).append(r)
    out: list[dict] = []
    for l in input[left]:
        matches = index.get(str(l.get(on)), [])
        if matches:
            out.extend({**m, **l} for m in matches)
        elif how == "left":
            out.append(dict(l))
    return out


@connector("concat")
def concat(input: Any) -> list[dict]:
    if isinstance(input, dict):
        return [r for rows in input.values() for r in rows]
    return single(input)


@connector("trigger")
def trigger(input: Any = None) -> list[dict]:
    """What started the run under `agentl serve`: the webhook body, the new files, or the
    schedule. The runner fills it; outside `agentl serve` it is empty."""
    return []
