"""Schema contracts (#122): a `schema:` on any step checks each record the step outputs against
the semantic types of the `cast` step, after the step runs.

- `on_bad: reject` (default): a record whose values are not already clean values of their type
  goes to the step's rejected records with `_error` (and `_raw`, the offending values).
- `on_bad: coerce`: values are converted as `cast` does ("12" -> 12, "R$ 1.234,56" -> 1234.56);
  what cannot be converted is rejected.
- `on_bad: fail`: the first bad record stops the run, naming the record and the field.

A field is required unless its type ends in `?` (`email?`): a missing or empty required value is
an error. `extra` decides what happens to fields the schema does not name: keep (default), drop,
or forbid (the record is bad)."""

from __future__ import annotations

from typing import Any

Records = list[dict[str, Any]]
_NUMERIC = ("int", "decimal", "float", "money", "percent")


class ContractError(ValueError):
    """`on_bad: fail` found a bad record."""


def parse(fields: dict[str, Any]) -> dict[str, tuple[str, dict]]:
    """Field -> (type name, options), with clear errors for unknown types."""
    from .connectors.transforms import _cast_spec

    if not isinstance(fields, dict) or not fields:
        raise ValueError("schema: map each field to a type, e.g. {id: int, email: email?}")
    return {str(f): _cast_spec(s, "schema") for f, s in fields.items()}


def _same(value: Any, clean: Any) -> bool:
    if isinstance(value, bool) != isinstance(clean, bool):
        return False
    if hasattr(clean, "isoformat"):
        clean = clean.isoformat()
    if hasattr(value, "isoformat"):
        value = value.isoformat()
    numeric = isinstance(value, (int, float)) and isinstance(clean, (int, float))
    return (numeric or type(value) is type(clean)) and value == clean


def validate(
    records: Records,
    fields: dict[str, Any],
    on_bad: str = "reject",
    extra: str = "keep",
    locale: str = "pt-BR",
    step: str = "",
    start: int = 0,
) -> tuple[Records, Records]:
    """(good, rejected). `start` is the index of the first record, for messages on chunks."""
    from . import types

    specs = parse(fields)
    orders: dict[str, str] = {}
    if on_bad == "coerce":
        us = locale.lower() in ("en-us", "en_us", "us")
        for f, (name, _) in specs.items():
            if name in ("date", "datetime"):
                orders[f] = types.date_order(r.get(f) for r in records) or ("mdy" if us else "dmy")
    good: Records = []
    bad: Records = []
    for i, r in enumerate(records):
        out = dict(r)
        errors: list[tuple[str, str]] = []
        for f in r:
            if f not in specs and extra == "forbid":
                errors.append((f, "not in the schema"))
        if extra == "drop":
            out = {k: v for k, v in out.items() if k in specs}
        for f, (name, opts) in specs.items():
            v = r.get(f)
            kw = {k: val for k, val in opts.items() if k != "_optional"}
            if name in _NUMERIC:
                kw.setdefault("locale", locale)
            if name in ("date", "datetime"):
                kw.setdefault("order", orders.get(f, "dmy"))
            try:
                clean = types.TYPES[name](v, **kw)
            except (ValueError, TypeError, ArithmeticError) as e:
                errors.append((f, str(e)))
                continue
            if clean is None:
                if not opts["_optional"]:
                    errors.append((f, "required" if f not in r else "empty, and the field is required"))
                elif f in r:
                    out[f] = None
                continue
            if on_bad == "coerce":
                out[f] = clean.isoformat() if hasattr(clean, "isoformat") else clean
            elif not _same(v, clean):
                errors.append((f, f"{v!r} is not a clean {name}"))
        if not errors:
            good.append(out)
            continue
        reason = "; ".join(f"{f}: {e}" for f, e in errors)
        if on_bad == "fail":
            where = f"step '{step}': " if step else ""
            raise ContractError(f"{where}schema: record {start + i}: {reason}")
        bad.append({**r, "_error": reason, "_raw": {f: r.get(f) for f, _ in errors}})
    return good, bad
