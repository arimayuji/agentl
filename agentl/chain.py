"""Pipeline chaining under `agentl serve`: `on_success` / `on_failure` / `after` actions that start
another pipeline or emit an event, `when` conditions over run metrics, and cycle checks.

    on_success:
      - trigger: process_report               # its `use: trigger` step gets one record: `with`
        with: { rows: "${outputs.extract}", day: "${trigger.day}" }
      - emit: { url: "${EVENTS_URL}", topic: report.ready }
        when: all_ok and rows_changed > 0
      - trigger: load_day                     # one child run per record
        foreach: "${outputs.days}"
        with: { day: "${item.day}" }
    after_all:                                # once every foreach child finished
      when: all_ok
      do: [{ use: email.send, with: { to: [desk@example.org], subject: "Loads done" } }]
    depends_on: [load_reference]              # its last run must have succeeded

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


def combine(children: list[dict[str, Any]]) -> dict[str, Any]:
    """The metrics of a group of runs (every `foreach` child) as one: all_ok only if each is,
    counts and cost summed."""
    ok = all(m["status"] == "succeeded" for m in children)
    status = "succeeded" if ok else "failed"
    return {
        "outcome": status,
        "status": status,
        "all_ok": all(m["all_ok"] for m in children),
        "failed": not ok,
        **{k: sum(m[k] for m in children) for k in ("rows_changed", "records", "rejected")},
        "cost_usd": sum(m["cost_usd"] or 0.0 for m in children),
    }


_REF = re.compile(r"\$\{(outputs|trigger|run|item)\.([A-Za-z0-9_]+)\}")


def resolve(value: Any, ctx: dict[str, Any]) -> Any:
    """Fill `${outputs.<step>}` (that step's records), `${trigger.<field>}` (first record that
    started the parent run), `${run.id|pipeline|status|error}` and, in a `foreach`,
    `${item.<field>}`. A value that is only a
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
        if kind == "item":
            return (ctx.get("item") or {}).get(key)
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


def items(expr: str, ctx: dict[str, Any]) -> list[dict]:
    """The records a `foreach` fans out over; a value that is not a record becomes `{value: ...}`."""
    got = resolve(expr, ctx)
    if got is None:
        return []
    if not isinstance(got, list):
        got = [got]
    return [g if isinstance(g, dict) else {"value": g} for g in got]


def call(use: str, params: dict[str, Any], record: dict) -> Any:
    """Run a connector once with one input record (an action's `use`, or emit's queue)."""
    from . import registry

    return registry.get(use)([record], **params)


def emit(cfg: Any, ctx: dict[str, Any], post: Any = None) -> dict:
    """POST the event, or send it with a queue connector (`use`). Its `id` (and the
    `Idempotency-Key` header) is `<run_id>:<topic>`, so a receiver can drop a repeat; on a queue,
    key or deduplicate by the `id` field (`key: id`, `dedup_field: id`)."""
    run = ctx["run"]
    event = {
        "id": f"{run['id']}:{cfg.topic}",
        "topic": cfg.topic,
        "pipeline": run["pipeline"],
        "run_id": run["id"],
        "status": run["status"],
        "payload": resolve(cfg.payload, ctx),
    }
    if cfg.use:
        call(cfg.use, resolve(cfg.with_, ctx), event)
        return event
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
        for a in actions(s):
            if a.trigger:
                out[s.name].add(a.trigger)
        for t in s.trigger:
            if t.after:
                out.setdefault(t.after, set()).add(s.name)
        for d in s.depends_on:  # the dependency runs (and is waited for) before this one
            out.setdefault(d, set()).add(s.name)
    return out


def actions(spec: Any) -> list[Any]:
    """Every action of a pipeline, `after_all` included."""
    return [*spec.on_success, *spec.on_failure, *spec.after, *(spec.after_all.do if spec.after_all else [])]


def check(specs: list[Any]) -> None:
    """Unknown pipeline names and cycles (a -> b -> a) are errors before anything runs."""
    names = {s.name for s in specs}
    for s in specs:
        if s.name in s.depends_on:
            raise ValueError(f"pipeline '{s.name}' depends on itself")
        if s.after_all and not any(a.foreach is not None for a in actions(s)):
            raise ValueError(f"pipeline '{s.name}': after_all needs a trigger action with foreach")
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
