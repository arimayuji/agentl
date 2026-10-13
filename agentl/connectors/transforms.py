"""Deterministic transforms that don't need an LLM."""

from __future__ import annotations

from typing import Any

from ..registry import connector
from ._util import single


@connector("select")
def select(input: Any, fields: list[str] | None = None, rename: dict[str, str] | None = None) -> list[dict]:
    out = []
    for r in single(input):
        row = {k: r.get(k) for k in fields} if fields else dict(r)
        for old, new in (rename or {}).items():
            if old in row:
                row[new] = row.pop(old)
        out.append(row)
    return out


@connector("filter")
def filter_(input: Any, where: dict[str, Any] | None = None, not_empty: list[str] | None = None) -> list[dict]:
    rows = single(input)
    where = where or {}
    return [r for r in rows
            if all(r.get(k) == v for k, v in where.items())
            and all(r.get(k) not in (None, "") for k in (not_empty or []))]


@connector("dedupe")
def dedupe(input: Any, on: list[str]) -> list[dict]:
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
    out = []
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
