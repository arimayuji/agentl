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
def dedupe(input: Any, on: str | list[str], keep: str | dict[str, str] = "first") -> list[dict]:
    """One record per `on` key. `keep: first` (default) or `last` keeps that one in input order;
    `keep: {latest: field}` keeps the record with the largest `field` (empty values lose)."""
    on = _names(on)
    latest = keep.get("latest") if isinstance(keep, dict) else None
    if latest is None and keep not in ("first", "last"):
        raise ValueError(f"dedupe keep must be first, last or {{latest: field}}, got {keep!r}")
    best: dict[tuple, dict] = {}
    for r in single(input):
        key = tuple(r.get(k) for k in on)
        if key not in best or keep == "last":
            best[key] = r
        elif latest is not None:
            cur, new = best[key].get(latest), r.get(latest)
            if not _blank(new) and (_blank(cur) or _sortable(new) > _sortable(cur)):
                best[key] = r
    return list(best.values())


def _sortable(v: Any) -> tuple:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return (0, v, "")
    try:
        return (0, float(str(v)), "")
    except ValueError:
        return (1, 0, str(v))


@connector("join")
def join(
    input: Any,
    left: str,
    right: str,
    on: str | list[str] | None = None,
    left_on: str | list[str] | None = None,
    right_on: str | list[str] | None = None,
    how: str = "inner",
    suffix: str = "_right",
    suffixes: list[str] | None = None,
) -> list[dict]:
    """Records of `left` with the matching records of `right`, on one key or several
    (`on: [a, b]`, or `left_on` / `right_on` when the names differ). `how`: inner, left, right
    or outer. A right column with the same name as a left one (other than a key) is kept as
    `<name><suffix>`; `suffixes: [_l, _r]` renames both sides instead."""
    if not isinstance(input, dict):
        raise ValueError("join needs two inputs: input: [left_step, right_step]")
    if how not in ("inner", "left", "right", "outer"):
        raise ValueError(f"join how must be inner, left, right or outer, got {how!r}")
    lk, rk = _names(left_on or on), _names(right_on or on)
    if not lk or len(lk) != len(rk):
        raise ValueError("join needs on, or left_on and right_on with the same number of fields")
    ls, rs = (suffixes or ["", suffix])[:2]
    lrows, rrows = list(input[left]), list(input[right])
    for side, rows, keys in (("left", lrows, lk), ("right", rrows, rk)):
        if rows and (missing := [k for k in keys if all(k not in r for r in rows)]):
            step = left if side == "left" else right
            raise ValueError(f"join key {', '.join(missing)} is not in the {side} input ({step!r})")
    lcols = {k for r in lrows for k in r} - set(lk)
    rcols = {k for r in rrows for k in r} - set(rk)
    clash = lcols & rcols

    def key(r: dict, keys: list[str]) -> tuple:
        return tuple(str(r.get(k)) for k in keys)

    def merge(l: dict | None, m: dict | None) -> dict:
        row: dict = {}
        if l is not None:
            row.update({(k + ls if k in clash else k): v for k, v in l.items()})
        else:  # right-only: its keys fill the left key names
            row.update({a: m.get(b) for a, b in zip(lk, rk, strict=True)})  # type: ignore[union-attr]
        if m is not None:
            for k, v in m.items():
                if k in rk:
                    if k not in lk and k not in row:
                        row[k] = v
                    continue
                row[k + rs if k in clash else k] = v
        return row

    index: dict[tuple, list[int]] = {}
    for i, r in enumerate(rrows):
        index.setdefault(key(r, rk), []).append(i)
    used: set[int] = set()
    out: list[dict] = []
    for l in lrows:
        matches = index.get(key(l, lk), [])
        for i in matches:
            used.add(i)
            out.append(merge(l, rrows[i]))
        if not matches and how in ("left", "outer"):
            out.append(merge(l, None))
    if how in ("right", "outer"):
        out.extend(merge(None, r) for i, r in enumerate(rrows) if i not in used)
    return out


@connector("flatten")
def flatten(input: Any, sep: str = ".", fields: str | list[str] | None = None) -> list[dict]:
    """Nested objects become columns: {"a": {"b": 1}} -> {"a.b": 1}. `fields` limits it to
    those columns. Lists are left as they are (see explode)."""
    only = set(_names(fields)) if fields else None

    def walk(prefix: str, v: Any, out: dict) -> None:
        if isinstance(v, dict) and v:
            for k, x in v.items():
                walk(f"{prefix}{sep}{k}", x, out)
        else:
            out[prefix] = v

    rows = []
    for r in single(input):
        row: dict = {}
        for k, v in r.items():
            if isinstance(v, dict) and (only is None or k in only):
                walk(k, v, row)
            else:
                row[k] = v
        rows.append(row)
    return rows


@connector("explode")
def explode(input: Any, field: str, keep_empty: bool = True) -> list[dict]:
    """One record per item of the list in `field`; the other columns repeat. An empty or missing
    list keeps the record with `field: null` unless `keep_empty: false`."""
    out = []
    for r in single(input):
        v = r.get(field)
        items = v if isinstance(v, (list, tuple)) else [v] if v is not None else []
        if not items:
            if keep_empty:
                out.append({**r, field: None})
            continue
        out.extend({**r, field: x} for x in items)
    return out


@connector("pivot")
def pivot(input: Any, index: str | list[str], columns: str, values: str, agg: str = "first") -> list[dict]:
    """Long to wide: one record per `index`, one column per distinct `columns` value holding
    `values`. Repeated cells are combined with `agg` (first, last, sum, count, avg, min, max)."""
    idx = _names(index)
    groups: dict[tuple, dict[str, list]] = {}
    names: list[str] = []
    for r in single(input):
        col = str(r.get(columns))
        if col not in names:
            names.append(col)
        groups.setdefault(tuple(r.get(k) for k in idx), {}).setdefault(col, []).append(r.get(values))
    if agg not in ("first", "last", "sum", "count", "count_distinct", "avg", "min", "max", "list"):
        raise ValueError(f"pivot agg {agg!r} is not one of first, last, sum, count, avg, min, max, list")
    out = []
    for k, cells in groups.items():
        row = dict(zip(idx, k, strict=True))
        for c in names:
            row[c] = _agg_value(agg, cells.get(c, []))
        out.append(row)
    return out


@connector("unpivot")
def unpivot(
    input: Any,
    columns: str | list[str],
    name: str = "variable",
    value: str = "value",
) -> list[dict]:
    """Wide to long: each of `columns` becomes its own record with `name` (the column) and
    `value`; the other columns repeat."""
    cols = _names(columns)
    out: list[dict] = []
    for r in single(input):
        rest = {k: v for k, v in r.items() if k not in cols}
        out.extend({**rest, name: c, value: r.get(c)} for c in cols if c in r)
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


def _cast_spec(spec: Any, label: str = "cast") -> tuple[str, dict]:
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
        main = {"money": "currency", "datetime": "tz", "phone": "country", "enum": "values"}.get(
            str(name).rstrip("?")
        )
        opts = {**({main: first} if main else {}), **rest}
    else:
        raise ValueError(f"{label}: cannot read the type {spec!r}")
    if not isinstance(name, str):
        raise ValueError(f"{label}: cannot read the type {spec!r}")
    optional = name.endswith("?")
    name = name.rstrip("?")
    if name not in types.TYPES:
        import difflib

        close = difflib.get_close_matches(name, list(types.TYPES), n=1)
        hint = f"; did you mean {close[0]!r}?" if close else ""
        raise ValueError(f"{label}: unknown type {name!r}{hint} Types: {', '.join(sorted(types.TYPES))}")
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


_EVERY = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _window(window: dict) -> tuple[str, int, bool]:
    if not isinstance(window, dict) or not window.get("field") or not window.get("every"):
        raise ValueError("aggregate: window needs field and every, e.g. {field: ts, every: 1m}")
    m = _EVERY.match(str(window["every"]))
    if not m or int(m.group(1)) <= 0:
        raise ValueError(
            f"aggregate: window every {window['every']!r} is not a duration; use e.g. 30s, 5m, 1h, 1d"
        )
    unknown = set(window) - {"field", "every", "end"}
    if unknown:
        raise ValueError(f"aggregate: unknown window option(s) {sorted(unknown)}; use field, every, end")
    return str(window["field"]), int(m.group(1)) * _UNIT[m.group(2)], bool(window.get("end", False))


def _window_start(value: Any, secs: int) -> Any:
    import datetime as dt

    from .. import types

    if isinstance(value, str) and re.fullmatch(r"\s*\d+(\.\d+)?\s*", value):
        value = float(value)  # epoch seconds or milliseconds written as text
    ts = types.to_datetime(value)
    if ts is None:
        raise ValueError("empty timestamp")
    if ts.tzinfo is None:
        epoch = (ts - dt.datetime(1970, 1, 1)).total_seconds()
        return dt.datetime(1970, 1, 1) + dt.timedelta(seconds=epoch // secs * secs)
    return dt.datetime.fromtimestamp(ts.timestamp() // secs * secs, ts.tzinfo)


def _order(v: Any) -> tuple:
    if v is None:
        return (2, "")
    return (0, v, "") if isinstance(v, (int, float)) and not isinstance(v, bool) else (1, str(v))


@connector("aggregate")
def aggregate(
    input: Any,
    metrics: dict[str, str],
    group_by: str | list[str] | None = None,
    window: dict | None = None,
) -> list[dict]:
    """Group records and summarise each group (#120): `group_by: [region]`, `metrics: {total:
    "sum(amount)", orders: "count()", customers: "count_distinct(customer)", avg_ticket:
    "avg(amount)"}`. Functions: sum, count, avg, min, max, count_distinct, first, last, list. Empty
    values are skipped. Groups come out in the order they first appear; no group_by = one record.

    Time windows: `window: {field: ts, every: 1m}` puts each record in a tumbling window of
    `every` (`30s`, `5m`, `1h`, `1d`) aligned to the Unix epoch (UTC), combined with `group_by`.
    The timestamp is ISO text, a datetime or epoch seconds/milliseconds. Each output record gets
    `window_start` (ISO 8601, keeping the input's offset; naive input stays naive) and, with
    `end: true`, `window_end`. Output is sorted by window, then group. A record whose timestamp
    cannot be read goes to the step's rejected records with `_error`."""
    keys = _names(group_by)
    win = _window(window) if window is not None else None
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
    bad: list[dict] = []
    for r in single(input):
        if win is None:
            groups.setdefault(tuple(r.get(k) for k in keys), []).append(r)
            continue
        try:
            start = _window_start(r.get(win[0]), win[1])
        except Exception as e:  # unreadable timestamp: reject the record, keep the rest
            bad.append({**r, "_error": f"{win[0]}: cannot read timestamp {r.get(win[0])!r} ({e})"})
            continue
        groups.setdefault((start, *(r.get(k) for k in keys)), []).append(r)

    def summary(group: dict, rows: list[dict]) -> dict:
        for name, (fn, field) in parsed.items():
            group[name] = (
                len(rows) if fn == "count" and not field else _agg_value(fn, [r.get(field) for r in rows])
            )
        return group

    if win is None:
        return [summary(dict(zip(keys, key, strict=True)), rows) for key, rows in groups.items()]
    import datetime as dt

    from ..registry import Rejects

    def order(item: tuple) -> tuple:
        start = item[0][0]
        epoch = start.timestamp() if start.tzinfo else (start - dt.datetime(1970, 1, 1)).total_seconds()
        return (epoch, *(_order(v) for v in item[0][1:]))

    out = []
    for key, rows in sorted(groups.items(), key=order):
        row: dict = {"window_start": key[0].isoformat()}
        if win[2]:
            row["window_end"] = (key[0] + dt.timedelta(seconds=win[1])).isoformat()
        row.update(zip(keys, key[1:], strict=True))
        out.append(summary(row, rows))
    return Rejects(out, bad)


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
