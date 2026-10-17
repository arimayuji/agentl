"""Layout drift (#178): a signature of a source's layout (columns in order and their types), kept
per pipeline and check step, and the difference when a source changes its format.

A check step with `on_drift:` computes the signature of its input. The first run records it; a
later run with a different layout emits `schema.drift` with the columns added, removed, probably
renamed and the type changes."""

from __future__ import annotations

import datetime as _dt
import difflib
import math
from typing import Any

# Columns renamed: a removed and an added column of the same type whose names are this similar
# (difflib ratio), or that sit at the same position.
RENAME_SIMILARITY = 0.6


def _kind(v: Any) -> str | None:
    if v is None or (isinstance(v, float) and math.isnan(v)) or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, _dt.datetime):
        return "datetime"
    if isinstance(v, _dt.date):
        return "date"
    if isinstance(v, str):
        return "str"
    if isinstance(v, (list, tuple)):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__


def _arrow_kind(t: Any) -> str:
    import pyarrow as pa

    checks = (
        (pa.types.is_boolean, "bool"),
        (pa.types.is_integer, "int"),
        (pa.types.is_floating, "float"),
        (pa.types.is_decimal, "float"),
        (pa.types.is_timestamp, "datetime"),
        (pa.types.is_date, "date"),
        (pa.types.is_string, "str"),
        (pa.types.is_large_string, "str"),
        (pa.types.is_list, "list"),
        (pa.types.is_large_list, "list"),
        (pa.types.is_struct, "dict"),
        (pa.types.is_null, "null"),
    )
    for test, name in checks:
        if test(t):
            return name
    return str(t)


def signature(data: Any) -> dict[str, Any]:
    """{columns: [names in order], types: {name: type}} for records or a pyarrow Table. Types are
    bool, int, float, str, date, datetime, list, dict; mixed columns join them ("int|str"), a
    column of ints and floats is float, and a column with no values is null."""
    if hasattr(data, "schema") and hasattr(data, "num_rows"):
        names = list(data.schema.names)
        return {"columns": names, "types": {f.name: _arrow_kind(f.type) for f in data.schema}}
    columns: list[str] = []
    seen: dict[str, set[str]] = {}
    for r in data:
        for k, v in r.items():
            if k not in seen:
                columns.append(k)
                seen[k] = set()
            kind = _kind(v)
            if kind:
                seen[k].add(kind)
    types: dict[str, str] = {}
    for c in columns:
        kinds = seen[c]
        if kinds == {"int", "float"}:
            kinds = {"float"}
        types[c] = "|".join(sorted(kinds)) or "null"
    return {"columns": columns, "types": types}


def _compatible(old: str, new: str) -> bool:
    # A column with no values in one of the runs says nothing about its type.
    return old == new or "null" in (old, new)


def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any] | None:
    """The drift from `before` to `after` as {added, removed, renamed, type_changed}, or None when
    the layout is the same. `renamed` lists [old, new] pairs; those columns are not repeated in
    added and removed. `type_changed` maps a column to [old type, new type]."""
    old_cols, new_cols = list(before["columns"]), list(after["columns"])
    old_t, new_t = before["types"], after["types"]
    removed = [c for c in old_cols if c not in new_cols]
    added = [c for c in new_cols if c not in old_cols]
    renamed: list[list[str]] = []
    for old in list(removed):
        best, score = None, 0.0
        for new in added:
            if not _compatible(old_t[old], new_t[new]):
                continue
            ratio = difflib.SequenceMatcher(None, old.lower(), new.lower()).ratio()
            same_place = old_cols.index(old) == new_cols.index(new)
            s = ratio + (1.0 if same_place else 0.0)
            if (ratio >= RENAME_SIMILARITY or same_place) and s > score:
                best, score = new, s
        if best is not None:
            renamed.append([old, best])
            removed.remove(old)
            added.remove(best)
    type_changed = {
        c: [old_t[c], new_t[c]] for c in old_cols if c in new_t and not _compatible(old_t[c], new_t[c])
    }
    if not (added or removed or renamed or type_changed):
        return None
    return {"added": added, "removed": removed, "renamed": renamed, "type_changed": type_changed}


def summary(d: dict[str, Any]) -> str:
    parts = []
    if d["added"]:
        parts.append("added " + ", ".join(d["added"]))
    if d["removed"]:
        parts.append("removed " + ", ".join(d["removed"]))
    if d["renamed"]:
        parts.append("renamed " + ", ".join(f"{a} -> {b}" for a, b in d["renamed"]))
    if d["type_changed"]:
        parts.append("type of " + ", ".join(f"{c} {a} -> {b}" for c, (a, b) in d["type_changed"].items()))
    return "; ".join(parts)
