"""A small, safe expression language for `compute` and friends (#120).

    total: "price * qty"
    nome: "upper(trim(nome))"
    faixa: "if(total > 1000, 'alta', 'baixa')"
    cep_ok: "regex(endereco, '\\d{5}-?\\d{3}') != null"

Expressions are parsed as Python expressions and then checked against a closed list of nodes and
functions: field names, numbers and strings, arithmetic, comparisons, `and` / `or` / `not`, and the
functions in FUNCTIONS. No attribute access, indexing, lambdas, comprehensions or imports, so a
pipeline file cannot run code through an expression. A field whose name is not an identifier is
read with `field("nome do campo")`. A missing field is null; arithmetic with null gives null."""

from __future__ import annotations

import ast
import datetime as dt
import math
import operator
import re
import unicodedata
from collections.abc import Callable
from functools import lru_cache
from typing import Any


class ExprError(ValueError):
    pass


def _null(*vals: Any) -> bool:
    return any(v is None for v in vals)


def _num(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return float(v) if any(c in v for c in ".eE") else int(v)
        except ValueError:
            raise ExprError(f"{v!r} is not a number") from None
    return v


def _arith(op: Callable[[Any, Any], Any]) -> Callable[[Any, Any], Any]:
    def run(a: Any, b: Any) -> Any:
        if _null(a, b):
            return None
        if op is operator.add and isinstance(a, str) and isinstance(b, str):
            return a + b
        try:
            return op(_num(a), _num(b))
        except ZeroDivisionError:
            return None

    return run


BINOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: _arith(operator.add),
    ast.Sub: _arith(operator.sub),
    ast.Mult: _arith(operator.mul),
    ast.Div: _arith(operator.truediv),
    ast.FloorDiv: _arith(operator.floordiv),
    ast.Mod: _arith(operator.mod),
}


def _cmp(op: Callable[[Any, Any], bool]) -> Callable[[Any, Any], Any]:
    def run(a: Any, b: Any) -> Any:
        if op in (operator.eq, operator.ne):
            return op(a, b)
        if _null(a, b):
            return None
        if isinstance(a, str) != isinstance(b, str):
            a, b = _num(a), _num(b)
        return op(a, b)

    return run


CMPOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Eq: _cmp(operator.eq),
    ast.NotEq: _cmp(operator.ne),
    ast.Lt: _cmp(operator.lt),
    ast.LtE: _cmp(operator.le),
    ast.Gt: _cmp(operator.gt),
    ast.GtE: _cmp(operator.ge),
    ast.In: lambda a, b: None if b is None else a in b,
    ast.NotIn: lambda a, b: None if b is None else a not in b,
}


# ---------------------------------------------------------------- functions
def _str(v: Any) -> str | None:
    return None if v is None else str(v)


def _strf(fn: Callable[[str], Any]) -> Callable[[Any], Any]:
    return lambda v: None if v is None else fn(str(v))


def _regex(v: Any, pattern: str, group: int = 0) -> Any:
    if v is None:
        return None
    m = re.search(pattern, str(v))
    return None if m is None else m.group(group)


def _date(v: Any) -> dt.date | None:
    if v is None or isinstance(v, dt.date):
        return v
    from .types import to_date

    return to_date(v)


def _date_diff(a: Any, b: Any, unit: str = "days") -> Any:
    da, db = _date(a), _date(b)
    if da is None or db is None:
        return None
    if isinstance(da, dt.datetime) and isinstance(db, dt.datetime):
        days = (da - db).total_seconds() / 86400
    else:  # a date and a datetime compare by their dates
        days = (
            (da.date() if isinstance(da, dt.datetime) else da)
            - (db.date() if isinstance(db, dt.datetime) else db)
        ).days
    return {"days": days, "weeks": days / 7, "months": days / 30.4375, "years": days / 365.25}[unit]


def _round(v: Any, digits: int = 0) -> Any:
    if v is None:
        return None
    r = round(float(_num(v)), int(digits))
    return int(r) if digits == 0 else r


def _len(v: Any) -> Any:
    return None if v is None else len(v) if isinstance(v, (str, list, dict)) else len(str(v))


def _coalesce(*vals: Any) -> Any:
    return next((v for v in vals if v is not None and v != ""), None)


def _fold(v: Any) -> Any:
    if v is None:
        return None
    return "".join(c for c in unicodedata.normalize("NFKD", str(v)) if not unicodedata.combining(c))


FUNCTIONS: dict[str, Callable[..., Any]] = {
    "upper": _strf(str.upper),
    "lower": _strf(str.lower),
    "trim": _strf(lambda s: re.sub(r"\s+", " ", s).strip()),
    "title": _strf(str.title),
    "unaccent": _fold,
    "replace": lambda v, old, new: None if v is None else str(v).replace(old, new),
    "regex": _regex,
    "regex_replace": lambda v, pattern, new: None if v is None else re.sub(pattern, new, str(v)),
    "substr": lambda v, start, length=None: (
        None if v is None else str(v)[start : None if length is None else start + length]
    ),
    "concat": lambda *vals: "".join("" if v is None else str(v) for v in vals),
    "split": lambda v, sep, n=None: (
        None if v is None else (str(v).split(sep) if n is None else (str(v).split(sep) + [None] * (n + 1))[n])
    ),
    "len": _len,
    "coalesce": _coalesce,
    "if": lambda cond, a, b=None: a if cond else b,  # written if(...), read as _if(...) (see _rewrite)
    "round": _round,
    "abs": lambda v: None if v is None else abs(_num(v)),
    "floor": lambda v: None if v is None else math.floor(_num(v)),
    "ceil": lambda v: None if v is None else math.ceil(_num(v)),
    "min": lambda *vals: min((v for v in vals if v is not None), default=None),
    "max": lambda *vals: max((v for v in vals if v is not None), default=None),
    "int": lambda v: None if v is None or v == "" else int(float(_num(v))),
    "float": lambda v: None if v is None or v == "" else float(_num(v)),
    "str": _str,
    "is_null": lambda v: v is None or v == "",
    "today": lambda: dt.date.today().isoformat(),
    "year": lambda v: None if (d := _date(v)) is None else d.year,
    "month": lambda v: None if (d := _date(v)) is None else d.month,
    "day": lambda v: None if (d := _date(v)) is None else d.day,
    "date_diff": _date_diff,
}
# `field` and `null` are handled by the evaluator itself.

_ALLOWED = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.Call,
    ast.Name,
    ast.Constant,
    ast.Load,
    ast.List,
    ast.Tuple,
    ast.And,
    ast.Or,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.IfExp,
    *BINOPS,
    *CMPOPS,
)


def _rewrite(text: str) -> str:
    """`if(...)` as a function: `if` is a Python keyword, so the token (never text inside quotes)
    becomes `_if` before parsing. Plain string literals are read raw, so regexes keep their
    backslashes."""
    import io
    import tokenize

    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError):
        return text
    out = []
    for i, t in enumerate(toks):
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if t.type == tokenize.NAME and t.string == "if" and nxt is not None and nxt.string == "(":
            t = t._replace(string="_if")
        elif t.type == tokenize.STRING and t.string[0] in "'\"":
            t = t._replace(string="r" + t.string)  # '\d' in a regex means \d, as people write it in YAML
        out.append(t)
    return tokenize.untokenize(out)


@lru_cache(maxsize=512)
def compile_expr(text: str) -> ast.Expression:
    """Parse and check once; raises ExprError naming what is not allowed."""
    try:
        tree = ast.parse(_rewrite(str(text).strip()), mode="eval")
    except SyntaxError as e:
        raise ExprError(f"cannot read the expression {text!r}: {e.msg}") from None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ExprError(f"{type(node).__name__} is not allowed in an expression: {text!r}")
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name == "_if":
                continue
            if name not in FUNCTIONS and name != "field":
                import difflib

                close = difflib.get_close_matches(str(name), [*FUNCTIONS, "field"], n=1)
                hint = f"; did you mean {close[0]}()?" if close else ""
                raise ExprError(f"unknown function {name!r} in {text!r}{hint}")
            if node.keywords:
                raise ExprError(f"keyword arguments are not supported: {text!r}")
    return tree


def evaluate(text: str, record: dict) -> Any:
    """The value of expression `text` for one record."""
    return _eval(compile_expr(text).body, record)


def _eval(node: ast.AST, rec: dict) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in ("null", "None"):
            return None
        if node.id in ("true", "True"):
            return True
        if node.id in ("false", "False"):
            return False
        return rec.get(node.id)
    if isinstance(node, ast.BinOp):
        return BINOPS[type(node.op)](_eval(node.left, rec), _eval(node.right, rec))
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, rec)
        if isinstance(node.op, ast.Not):
            return not v
        if v is None:
            return None
        return -_num(v) if isinstance(node.op, ast.USub) else _num(v)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            out: Any = True
            for v in node.values:
                out = _eval(v, rec)
                if not out:
                    return out
            return out
        for v in node.values:
            out = _eval(v, rec)
            if out:
                return out
        return out
    if isinstance(node, ast.Compare):
        left = _eval(node.left, rec)
        for op, right_node in zip(node.ops, node.comparators, strict=True):
            right = _eval(right_node, rec)
            ok = CMPOPS[type(op)](left, right)
            if not ok:
                return ok
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _eval(node.body, rec) if _eval(node.test, rec) else _eval(node.orelse, rec)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e, rec) for e in node.elts]
    if isinstance(node, ast.Call):
        assert isinstance(node.func, ast.Name)
        args = [_eval(a, rec) for a in node.args]
        if node.func.id == "field":
            return rec.get(str(args[0]))
        if node.func.id == "_if":
            return FUNCTIONS["if"](*args)
        try:
            return FUNCTIONS[node.func.id](*args)
        except ExprError:
            raise
        except (TypeError, ValueError, re.error) as e:
            raise ExprError(f"{node.func.id}(): {e}") from None
    raise ExprError(f"{type(node).__name__} is not allowed")  # unreachable after compile_expr
