"""`map_schema` (#177): map the columns of a semi-structured source onto target fields.

The mapping is inferred once per source layout (the header signature): first by name (normalised
names and `synonyms`), then, for the fields still unmatched, with one structured LLM call that sees
the header and a sample. It is kept in the run store and later runs on the same layout apply it with
no model call. A new layout fails clearly instead of being mapped silently. `mapping:` in the YAML
pins the mapping and skips inference altogether."""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Callable
from typing import Any

from .registry import Rejects, connector

# Header cells a reader fills in when the real cell is empty (pandas, openpyxl, our own readers).
_PLACEHOLDER = re.compile(r"^(unnamed[:_ ]?.*|col(umn)?_?\d+|field_?\d+|_?\d+)?$", re.IGNORECASE)
# `target` types that have another name in `cast`.
_CAST_ALIASES = {"str": "text", "string": "text", "number": "decimal"}


class MappingError(ValueError):
    """The step cannot map this input: a new layout, or a mapping waiting for review."""


def normalise(name: Any) -> str:
    """'Região (UF)' -> 'regiao_uf': lower case, no accents, non-alphanumerics to one `_`."""
    text = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def header(rows: list[dict], header_rows: int = 0) -> tuple[list[str], list[dict]]:
    """(column names, data rows). With `header_rows: N` the first N records are more header rows
    (a 2 or 3 row Excel header): each column's name joins its cells top to bottom with " / ", and an
    empty cell takes the value on its left (a merged cell)."""
    keys: list[str] = []
    for r in rows[: max(header_rows, 1) + 50]:
        keys += [k for k in r if k not in keys]
    if header_rows <= 0:
        return keys, rows
    levels: list[list[str]] = [["" if _PLACEHOLDER.match(str(k).strip()) else str(k).strip() for k in keys]]
    levels += [["" if _blank(r.get(k)) else str(r.get(k)).strip() for k in keys] for r in rows[:header_rows]]
    for level in levels[:-1]:  # merged cells in the upper rows: an empty cell takes the left value
        for i in range(1, len(level)):
            level[i] = level[i] or level[i - 1]
    names: list[str] = []
    for i in range(len(keys)):
        parts: list[str] = []
        for level in levels:
            if level[i] and (not parts or parts[-1] != level[i]):
                parts.append(level[i])
        name = " / ".join(parts) or f"column_{i + 1}"
        while name in names:
            name += "_"
        names.append(name)
    rename = dict(zip(keys, names, strict=True))
    data = [{rename[k]: v for k, v in r.items() if k in rename} for r in rows[header_rows:]]
    return names, data


def signature(columns: list[str]) -> str:
    """Hash of the layout: the column names in order."""
    return hashlib.sha256(json.dumps(columns).encode()).hexdigest()[:16]


def match(columns: list[str], target: dict[str, Any], synonyms: dict[str, Any] | None = None) -> dict:
    """{field: column or None} by name: the field or one of its synonyms equals a column, or the last
    part of a multi-row column name, once both are normalised."""
    by_name: dict[str, str] = {}
    for c in columns:
        by_name.setdefault(normalise(c), c)
        by_name.setdefault(normalise(c.split(" / ")[-1]), c)
    out: dict[str, str | None] = {}
    used: set[str] = set()
    for field in target:
        names = synonyms.get(field) if synonyms else None
        candidates = [field, *([names] if isinstance(names, str) else list(names or []))]
        hit = next(
            (by_name[n] for n in map(normalise, candidates) if n in by_name and by_name[n] not in used), None
        )
        out[field] = hit
        if hit:
            used.add(hit)
    return out


INSTRUCTION = (
    "Map the columns of a spreadsheet onto target fields. You get the column names, the target "
    "fields with their types, the fields already mapped, and sample rows. For each requested field "
    "answer with the exact name of the column that holds it, copied from `columns`, or null when no "
    "column fits. Do not guess: a wrong column is worse than null."
)


def infer(
    columns: list[str],
    data: list[dict],
    target: dict[str, Any],
    synonyms: dict[str, Any] | None,
    sample_rows: int,
    ask: Callable[[str, dict, type], Any] | None,
) -> tuple[dict, list[str]]:
    """({field: column or None}, fields the LLM mapped). `ask(instruction, record, schema)` is a
    provider's `structured`, called once and only when some field has no match by name."""
    from .llm import schema_model

    found = match(columns, target, synonyms)
    missing = [f for f, c in found.items() if c is None]
    if not missing or ask is None:
        return found, []
    taken = {c for c in found.values() if c}
    record = {
        "columns": [c for c in columns if c not in taken],
        "fields": {f: target[f] for f in missing},
        "already_mapped": {f: c for f, c in found.items() if c},
        "sample": data[:sample_rows],
    }
    model = schema_model("column_mapping", {f: "str?" for f in missing})
    value = ask(INSTRUCTION, record, model).model_dump()
    by_llm = []
    for f in missing:
        col = value.get(f)
        if isinstance(col, str) and col in record["columns"] and col not in taken:
            found[f] = col
            taken.add(col)
            by_llm.append(f)
    return found, by_llm


def apply(data: list[dict], columns: dict[str, str | None], target: dict[str, Any], **cast: Any) -> Rejects:
    """Records with the target fields, typed by `cast`; rows with every field empty are dropped."""
    from .connectors.transforms import cast as cast_step

    pairs = list(columns.items())
    rows = []
    for r in data:
        row = {f: (r.get(c) if c else None) for f, c in pairs}
        if not all(_blank(v) for v in row.values()):
            rows.append(row)
    types = {f: _CAST_ALIASES.get(t, t) if isinstance(t, str) else t for f, t in target.items()}
    out = cast_step(rows, types, **cast)
    return out if isinstance(out, Rejects) else Rejects(out)


def _drift(columns: list[str], known: dict[str, dict]) -> str:
    last = max(known.values(), key=lambda m: m.get("created", 0))
    before = last.get("header", [])
    added = [c for c in columns if c not in before]
    removed = [c for c in before if c not in columns]
    return f"new columns {added}, missing {removed}" if added or removed else "same columns in another order"


def run(
    rows: list[dict],
    target: dict[str, Any],
    *,
    load: Callable[[], dict | None] = lambda: None,
    save: Callable[[dict], None] = lambda _: None,
    ask: Callable[[str, dict, type], Any] | None = None,
    where: str = "",
    mapping: dict[str, str | None] | None = None,
    synonyms: dict[str, Any] | None = None,
    header_rows: int = 0,
    sample_rows: int = 30,
    review: str = "auto",
    on_drift: str = "fail",
    locale: str = "pt-BR",
    on_bad: str = "reject",
) -> tuple[Rejects, dict | None]:
    """(mapped records, the mapping inferred in this call or None). `load` and `save` read and
    write {"layouts": {signature: mapping}} for this step; `where` names the pipeline and step in
    error messages."""
    if review not in ("auto", "required"):
        raise ValueError("map_schema: review is auto or required")
    if on_drift not in ("fail", "infer"):
        raise ValueError("map_schema: on_drift is fail or infer")
    if not target:
        raise ValueError("map_schema: `target` needs at least one field")
    columns, data = header(list(rows), header_rows)
    cast = {"locale": locale, "on_bad": on_bad}
    if mapping is not None:
        unknown = [f for f in mapping if f not in target]
        absent = [c for c in mapping.values() if c and c not in columns]
        if unknown or absent:
            raise MappingError(
                f"map_schema: `mapping` does not fit this input: fields not in target {unknown}, "
                f"columns not in the input {absent}. Columns: {columns}"
            )
        return apply(data, {f: mapping.get(f) for f in target}, target, **cast), None
    sig = signature(columns)
    stored = load() or {}
    layouts: dict[str, dict] = dict(stored.get("layouts") or {})
    hit = layouts.get(sig)
    if hit is None:
        if layouts and on_drift == "fail":
            raise MappingError(
                f"map_schema: the input layout changed ({_drift(columns, layouts)}); it is not mapped "
                f"silently. Pin `mapping:` in the YAML, set `on_drift: infer`, or "
                f"`agentl mappings forget {where}` and run again. Columns: {columns}"
            )
        found, by_llm = infer(columns, data, target, synonyms, sample_rows, ask)
        hit = {
            "columns": found,
            "by_llm": by_llm,
            "header": columns,
            "approved": review == "auto",
            "created": time.time(),
        }
        layouts[sig] = hit
        save({"layouts": layouts})
        new: dict | None = {**hit, "signature": sig}
    else:
        new = None
    if not hit.get("approved") and review == "required":
        lines = "\n".join(f"  {f}: {c}" for f, c in hit["columns"].items())
        raise MappingError(
            f"map_schema: the mapping for layout {sig} waits for review:\n{lines}\n"
            f"Approve it with `agentl mappings approve {where}`, or pin `mapping:` in the YAML."
        )
    return apply(data, {f: hit["columns"].get(f) for f in target}, target, **cast), new


@connector("map_schema")
def map_schema(input: Any, target: dict[str, Any], **params: Any) -> list[dict]:
    """Map the columns of a semi-structured input (several header rows, varying names) onto `target`
    ({field: cast type}). By name first (`synonyms: {field: [names]}`), then one LLM call for the
    rest; the mapping is kept per layout in the run store and reused with no model call. `mapping:`
    ({field: column}) pins it. `header_rows`: extra header rows; `review`: auto or required (wait for
    `agentl mappings approve`); `on_drift`: fail or infer on a new layout; `locale`, `on_bad` as in
    `cast`. Called outside a run, it maps by name only."""
    from .connectors._util import single

    return run(single(input), target, **params)[0]
