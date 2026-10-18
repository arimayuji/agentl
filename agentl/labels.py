"""Pipeline labels: free-form `labels:` from the spec plus `source` and `sink` inferred from the
connector steps, recorded with each run so runs can be grouped and filtered (team, domain, ...).

    agentl runs --label team=sales-data
    agentl dashboard export site/ --label domain=orders"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

KEY = re.compile(r"^[A-Za-z_][\w.\-]{0,62}$")
MAX_VALUE = 200


def check(labels: dict[str, Any]) -> dict[str, str]:
    """Validate a `labels:` map: string keys made of letters, digits, `_ . -`; string values."""
    out: dict[str, str] = {}
    for k, v in labels.items():
        if not isinstance(k, str) or not KEY.match(k):
            raise ValueError(f"label key {k!r}: use letters, digits, '_', '.' or '-' (max 63)")
        if not isinstance(v, str) or isinstance(v, bool):
            raise ValueError(f"label {k}: the value must be a string, got {type(v).__name__}")
        if len(v) > MAX_VALUE or "\n" in v:
            raise ValueError(f"label {k}: one line of at most {MAX_VALUE} characters")
        out[k] = v
    return out


def _connector(use: str) -> str | None:
    return use.split(".", 1)[0] if "." in use else None


def infer(spec: Any) -> dict[str, str]:
    """`source`: connectors of `use:` steps with no input; `sink`: connectors of `use:` steps
    that take an input and feed nothing. Several values are joined with commas, sorted."""
    consumed = {i for s in spec.steps for i in s.inputs}
    sources: set[str] = set()
    sinks: set[str] = set()
    for s in spec.steps:
        name = _connector(s.use) if s.use else None
        if not name:
            continue
        if not s.inputs:
            sources.add(name)
        elif s.id not in consumed:
            sinks.add(name)
    out = {}
    if sources:
        out["source"] = ",".join(sorted(sources))
    if sinks:
        out["sink"] = ",".join(sorted(sinks))
    return out


def of(spec: Any) -> dict[str, str]:
    """The labels recorded with a run: inferred ones, overridden by the spec's own."""
    return {**infer(spec), **(spec.labels or {})}


def parse_filters(items: Iterable[str] | None) -> dict[str, str]:
    """`["team=sales", "domain=orders"]` -> {"team": "sales", "domain": "orders"}."""
    out: dict[str, str] = {}
    for item in items or ():
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--label {item!r}: expected key=value")
        out[key.strip()] = value.strip()
    return out


def matches(run_labels: dict[str, str] | None, filters: dict[str, str] | None) -> bool:
    """Every filter must match. A comma-joined inferred value (`source: csv,http`) matches
    either part."""
    have = run_labels or {}
    for k, v in (filters or {}).items():
        value = have.get(k)
        if value is None or (value != v and v not in value.split(",")):
            return False
    return True
