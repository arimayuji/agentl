"""Human review queue (#153): agent answers a `review:` rule flags wait for a person.

    agentl review list [--pipeline P] [--step S] [--status pending|accepted|fixed|rejected|all]
    agentl review show ID
    agentl review accept ID...
    agentl review fix ID field=value... | --value '{"field": ...}'
    agentl review reject ID...
    agentl review export --golden FILE [--pipeline P] [--step S]

An accepted or fixed answer is stored under the record's agent cache key: the next run that sees
the same input uses it with no llm call, and `agentl review export` appends it to a golden set
(#154) for `agentl eval`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# A model's self-reported confidence is not part of the expected answer of a golden record.
NOT_EXPECTED = ("confidence",)


def parse_assignments(items: list[str], value: str | None) -> dict[str, Any]:
    """`field=value` pairs (values read as JSON when they parse, else as text) and/or a JSON
    object from `--value`."""
    out: dict[str, Any] = {}
    if value is not None:
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError as e:
            raise ValueError(f"--value is not valid JSON: {e.msg}") from None
        if not isinstance(loaded, dict):
            raise ValueError('--value must be a JSON object, e.g. \'{"category": "travel"}\'')
        out.update(loaded)
    for item in items:
        name, sep, raw = item.partition("=")
        if not sep or not name:
            raise ValueError(f"expected field=value, got {item!r}")
        try:
            out[name] = json.loads(raw)
        except json.JSONDecodeError:
            out[name] = raw
    return out


def fix(store: Any, review_id: int, changes: dict[str, Any]) -> dict:
    """Correct a queued answer: `changes` replace fields of the model's answer. Fields the step
    does not output are refused."""
    row = store.review_get(review_id)
    if row is None:
        raise KeyError(review_id)
    if not changes:
        raise ValueError("fix needs at least one field=value (or --value)")
    unknown = [k for k in changes if k not in row["answer"]]
    if unknown:
        known = ", ".join(row["answer"]) or "none"
        raise ValueError(f"not output fields of step '{row['step']}': {', '.join(unknown)} (fields: {known})")
    value = {**row["answer"], **changes}
    store.review_resolve(review_id, "fixed", value)
    return value


def accept(store: Any, review_id: int) -> dict:
    row = store.review_get(review_id)
    if row is None:
        raise KeyError(review_id)
    store.review_resolve(review_id, "accepted", row["answer"])
    return dict(row["answer"])


def reject(store: Any, review_id: int) -> None:
    if store.review_get(review_id) is None:
        raise KeyError(review_id)
    store.review_resolve(review_id, "rejected", None)


def golden_lines(rows: list[dict]) -> list[dict]:
    """Golden records (#154) from reviewed rows: the step's input plus the answer a person
    accepted or fixed, without the model's confidence."""
    out = []
    for r in rows:
        if r["status"] not in ("accepted", "fixed") or not isinstance(r["value"], dict):
            continue
        expected = {k: v for k, v in r["value"].items() if k not in NOT_EXPECTED}
        out.append({**(r["input"] or {}), **expected})
    return out


def export_golden(store: Any, path: str | Path, pipeline: str | None = None, step: str | None = None) -> int:
    """Append the reviewed answers to the golden file at `path`, skipping lines it already has.
    Returns how many lines were added."""
    rows = [r for s in ("accepted", "fixed") for r in store.review_list(pipeline, step, status=s)]
    rows.sort(key=lambda r: r["id"])
    target = Path(path)
    seen: set[str] = set()
    if target.exists():
        for line in target.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    seen.add(json.dumps(json.loads(line), sort_keys=True, default=str))
                except json.JSONDecodeError:
                    continue
    new = []
    for rec in golden_lines(rows):
        k = json.dumps(rec, sort_keys=True, default=str)
        if k not in seen:
            seen.add(k)
            new.append(json.dumps(rec, ensure_ascii=False, default=str))
    if new:
        target.parent.mkdir(parents=True, exist_ok=True)
        prefix = ""
        if target.exists():
            text = target.read_text(encoding="utf-8")
            prefix = "" if not text or text.endswith("\n") else "\n"
        with target.open("a", encoding="utf-8") as f:
            f.write(prefix + "\n".join(new) + "\n")
    return len(new)
