"""Deterministic transforms that don't need an LLM."""

from __future__ import annotations

import re
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
    names = _names(fields)  # once, not per record: this runs on millions of records
    renames = list((rename or {}).items())
    rows = single(input)
    if names and not renames:
        return [{k: r.get(k) for k in names} for r in rows]
    out = []
    for r in rows:
        row = {k: r.get(k) for k in names} if names else dict(r)
        for old, new in renames:
            if old in row:
                row[new] = row.pop(old)
        out.append(row)
    return out


@connector("filter")
def filter_(
    input: Any, where: dict[str, Any] | None = None, not_empty: str | list[str] | None = None
) -> list[dict]:
    """Keep records whose `where` fields equal the given values (`where: { status: paid }`) and
    whose `not_empty` fields have a value. Comparisons such as `total > 100` need a `fn:` step."""
    rows = single(input)
    conds = list((where or {}).items())
    filled = _names(not_empty)  # once, not per record
    if not conds and len(filled) == 1:  # the common case, without the generator per record
        (k,) = filled
        return [r for r in rows if r.get(k) not in (None, "")]
    return [
        r
        for r in rows
        if all(r.get(k) == v for k, v in conds) and all(r.get(k) not in (None, "") for k in filled)
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
def join(
    input: Any, left: str, right: str, on: str, how: str = "inner", suffix: str = "_right"
) -> list[dict]:
    """Records of `left` with the matching records of `right`. A right column with the same name
    as a left one (other than `on`) is kept as `<name><suffix>` instead of being dropped."""
    if not isinstance(input, dict):
        raise ValueError("join needs two inputs: input: [left_step, right_step]")
    index: dict[Any, list[dict]] = {}
    for r in input[right]:
        index.setdefault(str(r.get(on)), []).append(r)
    out: list[dict] = []
    for l in input[left]:
        matches = index.get(str(l.get(on)), [])
        if matches:
            for m in matches:
                row = dict(l)
                for k, v in m.items():
                    if k not in l:
                        row[k] = v
                    elif k != on:
                        row[k + suffix] = v
                out.append(row)
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


def _cast_spec(spec: Any) -> tuple[str, dict]:
    """`date` | `{money: BRL}` | `{enum: [a, b], synonyms: {...}}` | `{type: date, ...}` ->
    (type name, options)."""
    from .. import types

    if isinstance(spec, str):
        name, opts = spec, {}
    elif isinstance(spec, dict) and "type" in spec:
        name, opts = spec["type"], {k: v for k, v in spec.items() if k != "type"}
    elif isinstance(spec, dict) and spec:
        name, first = next(iter(spec.items()))
        rest = {k: v for k, v in spec.items() if k != name}
        main = {"money": "currency", "datetime": "tz", "phone": "country", "enum": "values"}.get(name)
        opts = {**({main: first} if main else {}), **rest}
    else:
        raise ValueError(f"cast: cannot read the type {spec!r}")
    optional = name.endswith("?")
    name = name.rstrip("?")
    if name not in types.TYPES:
        import difflib

        close = difflib.get_close_matches(name, list(types.TYPES), n=1)
        hint = f"; did you mean {close[0]!r}?" if close else ""
        raise ValueError(f"cast: unknown type {name!r}{hint} Types: {', '.join(sorted(types.TYPES))}")
    return name, {**opts, "_optional": optional}


@connector("cast")
def cast(
    input: Any,
    fields: dict[str, Any],
    locale: str = "pt-BR",
    on_bad: str = "reject",
    strict: bool = False,
) -> list[dict]:
    """Turn messy values into clean typed ones (#119). `fields` maps a field to a type: date,
    datetime, int, decimal, money, percent, bool, enum, text, name, email, cpf, cnpj, cep, uf,
    phone (`{money: BRL}`, `{datetime: America/Sao_Paulo}`, `{enum: [a, b], synonyms: {a: [x]}}`).
    Numbers follow `locale` (pt-BR: 1.234,56). Dates are read in one order per column: day first
    when a value says so (25/02), month first likewise; a column where nothing tells uses the
    locale, or fails with `strict: true`. Empty values become null. `on_bad`: reject (the record
    goes to the step's rejected records with `_error` and `_raw`), fail (stop the run), or keep
    (leave the original value). Dates and times come out as ISO text."""
    from .. import types
    from ..registry import Rejects

    if on_bad not in ("reject", "fail", "keep"):
        raise ValueError("cast: on_bad is reject, fail or keep")
    rows = single(input)
    specs = {f: _cast_spec(s) for f, s in fields.items()}
    us = locale.lower() in ("en-us", "en_us", "us")
    orders: dict[str, str] = {}
    for f, (name, _) in specs.items():
        if name in ("date", "datetime"):
            found = types.date_order(r.get(f) for r in rows)
            if found is None and strict:
                values = [v for r in rows if isinstance(v := r.get(f), str)]
                if any(types.NUMERIC.match(v.strip()) for v in values):
                    raise ValueError(
                        f"cast: the dates in {f!r} could be day-first or month-first; "
                        "no value tells (set strict: false to use the locale)"
                    )
            orders[f] = found or ("mdy" if us else "dmy")
    good: list[dict] = []
    bad: list[dict] = []
    for r in rows:
        out = dict(r)
        errors = []
        for f, (name, opts) in specs.items():
            v = r.get(f)
            kw = {k: val for k, val in opts.items() if k != "_optional"}
            if name in ("int", "decimal", "float", "money", "percent"):
                kw.setdefault("locale", locale)
            if name in ("date", "datetime"):
                kw.setdefault("order", orders[f])
            try:
                clean = types.TYPES[name](v, **kw)
            except (ValueError, TypeError, ArithmeticError) as e:
                if on_bad == "fail":
                    raise ValueError(f"cast: field {f!r}: {e}") from None
                errors.append((f, str(e)))
                continue
            if hasattr(clean, "isoformat"):
                clean = clean.isoformat()
            out[f] = clean
        if errors and on_bad == "reject":
            bad.append(
                {
                    **r,
                    "_error": "; ".join(f"{f}: {e}" for f, e in errors),
                    "_raw": {f: r.get(f) for f, _ in errors},
                }
            )
        else:
            good.append(out)
    return Rejects(good, bad)


@connector("compute")
def compute(input: Any, fields: dict[str, str], on_error: str = "reject") -> list[dict]:
    """New or replaced fields from safe expressions (#120): `fields: {total: "price * qty",
    nome: "upper(trim(nome))", faixa: "if(total > 1000, 'alta', 'baixa')"}`. Fields are computed
    in order, so a later one can use an earlier one. Arithmetic, comparisons, and/or/not and the
    functions in agentl.expr (upper, lower, trim, replace, regex, coalesce, if, round, date_diff,
    ...); nothing else runs. `on_error`: reject (the record goes to the rejected records with the
    reason), fail, or null (the field is set to null)."""
    from .. import expr
    from ..registry import Rejects

    if on_error not in ("reject", "fail", "null"):
        raise ValueError("compute: on_error is reject, fail or null")
    for text in fields.values():
        expr.compile_expr(str(text))  # a bad expression fails before any record
    good: list[dict] = []
    bad: list[dict] = []
    for r in single(input):
        out = dict(r)
        error = None
        for name, text in fields.items():
            try:
                out[name] = expr.evaluate(str(text), out)
            except expr.ExprError as e:
                if on_error == "fail":
                    raise ValueError(f"compute: field {name!r}: {e}") from None
                if on_error == "null":
                    out[name] = None
                    continue
                error = f"{name}: {e}"
                break
        if error:
            bad.append({**r, "_error": error})
        else:
            good.append(out)
    return Rejects(good, bad)


_AGG = re.compile(r"^\s*(sum|count|avg|mean|min|max|count_distinct|first|last|list)\s*\(\s*([^)]*?)\s*\)\s*$")


def _agg_value(fn: str, values: list) -> Any:
    present = [v for v in values if v is not None and v != ""]
    if fn == "count":
        return len(present)
    if fn == "count_distinct":
        return len({repr(v) for v in present})
    if fn == "first":
        return present[0] if present else None
    if fn == "last":
        return present[-1] if present else None
    if fn == "list":
        return present
    if not present:
        return None
    if fn in ("min", "max"):
        return (min if fn == "min" else max)(present)
    nums = []
    for v in present:
        try:
            nums.append(float(v) if isinstance(v, str) else v)
        except ValueError:
            raise ValueError(f"aggregate: {fn}() needs numbers, got {v!r}") from None
    total = sum(nums)
    if fn == "sum":
        return total
    return total / len(nums)


@connector("aggregate")
def aggregate(input: Any, metrics: dict[str, str], group_by: str | list[str] | None = None) -> list[dict]:
    """Group records and summarise each group (#120): `group_by: [region]`, `metrics: {total:
    "sum(amount)", orders: "count()", customers: "count_distinct(customer)", avg_ticket:
    "avg(amount)"}`. Functions: sum, count, avg, min, max, count_distinct, first, last, list. Empty
    values are skipped. Groups come out in the order they first appear; no group_by = one record."""
    keys = _names(group_by)
    parsed = {}
    for name, spec in metrics.items():
        m = _AGG.match(str(spec))
        if not m:
            raise ValueError(f"aggregate: cannot read {spec!r}; use e.g. sum(amount) or count()")
        fn, field = m.group(1).replace("mean", "avg"), m.group(2)
        if not field and fn != "count":
            raise ValueError(f"aggregate: {fn}() needs a field")
        parsed[name] = (fn, field)
    groups: dict[tuple, list[dict]] = {}
    for r in single(input):
        groups.setdefault(tuple(r.get(k) for k in keys), []).append(r)
    out = []
    for key, rows in groups.items():
        row = dict(zip(keys, key, strict=True))
        for name, (fn, field) in parsed.items():
            row[name] = (
                len(rows) if fn == "count" and not field else _agg_value(fn, [r.get(field) for r in rows])
            )
        out.append(row)
    return out


def _sort_keys(by: str | list[Any] | dict[str, str]) -> list[tuple[str, bool]]:
    if isinstance(by, dict):
        return [(f, str(d).lower() in ("desc", "descending")) for f, d in by.items()]
    items = [by] if isinstance(by, str) else by
    out = []
    for item in items:
        if isinstance(item, dict):
            out += _sort_keys(item)
        else:
            s = str(item)
            out.append((s[1:], True) if s.startswith("-") else (s, False))
    return out


@connector("sort")
def sort(input: Any, by: str | list[Any] | dict[str, str]) -> list[dict]:
    """Order records (#120): `by: [region, -total]` (a leading - is descending) or `by: {total:
    desc}`. Empty values go last either way; numbers and text never compare to each other."""
    rows = list(single(input))
    for field, desc in reversed(_sort_keys(by)):
        present = [r for r in rows if r.get(field) not in (None, "")]
        empty = [r for r in rows if r.get(field) in (None, "")]

        def key(r: dict, f: str = field) -> tuple:
            v = r.get(f)
            return (0, v) if isinstance(v, (int, float)) and not isinstance(v, bool) else (1, str(v))

        rows = sorted(present, key=key, reverse=desc) + empty
    return rows


@connector("limit")
def limit(input: Any, n: int, offset: int = 0) -> list[dict]:
    """The first `n` records (after skipping `offset`)."""
    return single(input)[offset : offset + n]


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


@connector("fill")
def fill(input: Any, values: dict[str, Any]) -> list[dict]:
    """Defaults for empty fields (#120): `values: {country: BR, discount: 0}`. A field that is
    missing, null or blank text gets the default; anything else is kept."""
    return [{**r, **{k: v for k, v in values.items() if _blank(r.get(k))}} for r in single(input)]
