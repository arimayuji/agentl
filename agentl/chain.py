"""Pipeline chaining under `agentl serve`: `on_success` / `on_failure` / `after` actions that start
another pipeline or emit an event, `when` conditions over run metrics, and cycle checks.

    on_success:
      - trigger: process_report               # its `use: trigger` step gets one record: `with`
        with: { rows: "${outputs.extract}", day: "${trigger.day}" }
      - emit: { url: "${EVENTS_URL}", topic: report.ready }
        when: all_ok and rows_changed > 0

Standard library only."""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import re
from typing import Any

# metric names a `when` can read
METRICS = ("outcome", "status", "all_ok", "failed", "rows_changed", "records", "rejected", "cost_usd")
_ALLOWED = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.Compare,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
)


def check_when(expr: str) -> ast.Expression:
    """Parse a `when`: metric names, numbers, strings, comparisons, and/or/not. Nothing else."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"invalid 'when': {expr!r}") from e
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ValueError(f"invalid 'when': {expr!r} (only comparisons, and, or, not)")
        if isinstance(node, ast.Name) and node.id not in METRICS:
            raise ValueError(f"'when': unknown metric '{node.id}' (use one of {', '.join(METRICS)})")
    return tree


def evaluate(expr: str, metrics: dict[str, Any]) -> bool:
    tree = check_when(expr)

    def ev(n: ast.AST) -> Any:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            return metrics.get(n.id)
        if isinstance(n, ast.UnaryOp):
            return not ev(n.operand)
        if isinstance(n, ast.BoolOp):
            vals = (ev(v) for v in n.values)
            return all(vals) if isinstance(n.op, ast.And) else any(vals)
        assert isinstance(n, ast.Compare)
        left = ev(n.left)
        for op, right_node in zip(n.ops, n.comparators, strict=True):
            right = ev(right_node)
            try:
                ok = {
                    ast.Eq: lambda a, b: a == b,
                    ast.NotEq: lambda a, b: a != b,
                    ast.Lt: lambda a, b: a < b,
                    ast.LtE: lambda a, b: a <= b,
                    ast.Gt: lambda a, b: a > b,
                    ast.GtE: lambda a, b: a >= b,
                }[type(op)](left, right)
            except TypeError:
                return False  # a missing metric compares as false
            if not ok:
                return False
            left = right
        return True

    return bool(ev(tree))


def metrics(result: Any) -> dict[str, Any]:
    """What a `when` can read from a finished run (a RunResult, or None if the run crashed)."""
    status = getattr(result, "status", "failed") if result is not None else "failed"
    writes = getattr(result, "writes", {}) or {}
    changed = sum(int(w.get(k, 0)) for w in writes.values() for k in ("inserted", "updated", "deleted"))
    outputs = getattr(result, "outputs", {}) or {}
    rejected = getattr(result, "rejected", {}) or {}
    return {
        "outcome": status,
        "status": status,
        "all_ok": status == "succeeded" and not any(len(v) for v in rejected.values()),
        "failed": status != "succeeded",
        "rows_changed": changed,
        "records": sum(len(v) for v in outputs.values()),
        "rejected": sum(len(v) for v in rejected.values()),
        "cost_usd": getattr(result, "cost_usd", 0.0),
    }


_REF = re.compile(r"\$\{(outputs|trigger|run)\.([A-Za-z0-9_]+)\}")


def resolve(value: Any, ctx: dict[str, Any]) -> Any:
    """Fill `${outputs.<step>}` (that step's records), `${trigger.<field>}` (first record that
    started the parent run) and `${run.id|pipeline|status|error}`. A value that is only a
    reference keeps its type; inside text it is formatted."""
    if isinstance(value, dict):
        return {k: resolve(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, ctx) for v in value]
    if not isinstance(value, str):
        return value

    def look(kind: str, key: str) -> Any:
        if kind == "outputs":
            return [dict(r) for r in ctx["outputs"].get(key, [])]
        if kind == "trigger":
            first = ctx["trigger"][0] if ctx["trigger"] else {}
            return first.get(key)
        return ctx["run"].get(key)

    whole = _REF.fullmatch(value)
    if whole:
        return look(*whole.groups())
    return _REF.sub(lambda m: str(look(*m.groups())), value)


def context(spec: Any, result: Any, trigger: list[dict]) -> dict[str, Any]:
    return {
        "outputs": getattr(result, "outputs", {}) or {},
        "trigger": trigger,
        "run": {
            "id": getattr(result, "run_id", ""),
            "pipeline": spec.name,
            "status": metrics(result)["status"],
            "error": getattr(result, "error", None),
        },
    }


def emit(cfg: Any, ctx: dict[str, Any], post: Any = None) -> dict:
    """POST the event. Its `id` (and the `Idempotency-Key` header) is `<run_id>:<topic>`, so a
    receiver can drop a repeat."""
    run = ctx["run"]
    event = {
        "id": f"{run['id']}:{cfg.topic}",
        "topic": cfg.topic,
        "pipeline": run["pipeline"],
        "run_id": run["id"],
        "status": run["status"],
        "payload": resolve(cfg.payload, ctx),
    }
    headers = {"Idempotency-Key": event["id"]}
    if cfg.secret:
        body = json.dumps(event, default=str).encode()
        headers["X-Signature-256"] = (
            "sha256=" + hmac.new(cfg.secret.encode(), body, hashlib.sha256).hexdigest()
        )
    if post is None:
        from .notify import _post as post
    post(cfg.url, event, headers)
    return event


def edges(specs: list[Any]) -> dict[str, set[str]]:
    """Pipeline -> pipelines a finished run of it can start."""
    out: dict[str, set[str]] = {s.name: set() for s in specs}
    for s in specs:
        for a in [*s.on_success, *s.on_failure, *s.after]:
            if a.trigger:
                out[s.name].add(a.trigger)
        for t in s.trigger:
            if t.after:
                out.setdefault(t.after, set()).add(s.name)
    return out


def check(specs: list[Any]) -> None:
    """Unknown pipeline names and cycles (a -> b -> a) are errors before anything runs."""
    names = {s.name for s in specs}
    graph = edges(specs)
    for src, dsts in graph.items():
        for name in sorted({src, *dsts} - names):
            raise ValueError(f"pipeline '{name}' is chained but not served; pass its YAML to agentl serve")
    state: dict[str, int] = {}

    def visit(n: str, path: list[str]) -> None:
        state[n] = 1
        for m in sorted(graph.get(n, ())):
            if state.get(m) == 1:
                cycle = [*path[path.index(m) :], m] if m in path else [n, m]
                raise ValueError(f"pipelines chain in a cycle: {' -> '.join(cycle)}")
            if not state.get(m):
                visit(m, [*path, m])
        state[n] = 2

    for n in sorted(graph):
        if not state.get(n):
            visit(n, [n])
