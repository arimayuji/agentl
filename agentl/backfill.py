"""Backfill: one run per file or per period, resumable.

    agentl backfill pipeline.yaml --glob 'reports/**/*.xlsx' --param-name path --concurrency 4
    agentl backfill pipeline.yaml --from 2026-01-01 --to 2026-03-31 --every day --param-name day

Each item becomes one run with the item passed as a run parameter. What finished is recorded
in the run store after every run, so starting the same backfill again skips it."""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .spec import ParamSpec, SpecError, load
from .store import open_store

EVERY = ("day", "week", "month")


def periods(start: str, end: str, every: str = "day") -> list[str]:
    """ISO dates from `start` to `end`, both included: each day, each 7 days or each month start."""
    if every not in EVERY:
        raise ValueError(f"--every: expected one of {EVERY}, got {every!r}")
    try:
        lo, hi = _dt.date.fromisoformat(start), _dt.date.fromisoformat(end)
    except ValueError as e:
        raise ValueError(f"--from/--to: {e}") from None
    if hi < lo:
        raise ValueError(f"--to {end} is before --from {start}")
    out = []
    cur = lo
    while cur <= hi:
        out.append(cur.isoformat())
        if every == "day":
            cur += _dt.timedelta(days=1)
        elif every == "week":
            cur += _dt.timedelta(days=7)
        else:
            y, m = divmod(cur.month, 12)
            cur = cur.replace(year=cur.year + y, month=m + 1, day=1)
    return out


def files(pattern: str, storage_options: dict | None = None) -> list[str]:
    """Paths matching a local or remote (fsspec) glob, sorted."""
    from .triggers import list_files

    return sorted(f["path"] for f in list_files(pattern, storage_options))


@dataclass
class Report:
    pipeline: str
    param: str
    total: int = 0
    skipped: list[str] = field(default_factory=list)
    succeeded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)  # item -> error
    runs: dict[str, str] = field(default_factory=dict)  # item -> run id

    @property
    def ok(self) -> bool:
        return not self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline": self.pipeline,
            "param": self.param,
            "total": self.total,
            "skipped": len(self.skipped),
            "succeeded": len(self.succeeded),
            "failed": self.failed,
            "runs": self.runs,
        }

    def text(self) -> str:
        lines = [
            f"backfill {self.pipeline}: {self.total} item(s), {len(self.succeeded)} succeeded, "
            f"{len(self.skipped)} already done, {len(self.failed)} failed"
        ]
        lines += [f"  failed {item}: {err}" for item, err in self.failed.items()]
        return "\n".join(lines)


def _pick_param(declared: dict, want: str) -> str:
    """The parameter that receives each item when --param-name is not given: the only one of
    type `want` (date for periods, str for files)."""
    match = [k for k, p in declared.items() if p.type == want]
    if len(match) == 1:
        return match[0]
    raise SpecError(
        f"pass --param-name: the pipeline declares {len(match)} {want} param(s) "
        f"({', '.join(match) or 'none'}), so it is not clear which one gets each item"
    )


def _declared(path: Path) -> dict[str, ParamSpec]:
    import yaml

    from .spec import _Loader, _param_spec

    raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)
    declared = raw.get("params") if isinstance(raw, dict) else None
    return {k: _param_spec(k, v) for k, v in (declared or {}).items()}


def _ledger_key(name: str, param: str, fixed: dict[str, Any]) -> str:
    fixed_txt = repr(sorted(fixed.items()))
    return "backfill:" + hashlib.sha256(f"{name}\0{param}\0{fixed_txt}".encode()).hexdigest()


def backfill(
    pipeline: str | os.PathLike,
    items: list[str],
    *,
    param: str | None = None,
    params: dict[str, Any] | None = None,
    vars: dict[str, str] | None = None,
    db: str | os.PathLike | None = None,
    concurrency: int = 1,
    redo: bool = False,
    kind: str = "str",
    on_done: Callable[[str, str, str | None], None] | None = None,
) -> Report:
    """Run `pipeline` once per item (a file path or an ISO date), `concurrency` at a time.
    Items finished in an earlier call are skipped unless `redo`. Every parameter is validated
    for every item before the first run starts."""
    from .runner import run

    if concurrency < 1:
        raise ValueError("--concurrency must be at least 1")
    env = {**os.environ, **(vars or {})}
    fixed = dict(params or {})
    declared = _declared(Path(pipeline))
    param = param or _pick_param(declared, kind)
    if param not in declared:
        raise SpecError(f"--param-name {param!r} is not declared under params: ({', '.join(declared)})")
    name = ""
    for item in items:  # fail fast: a bad date or a missing param stops before any run
        name = load(Path(pipeline), env, {**fixed, param: item}).name

    report = Report(name, param, total=len(items))
    key = _ledger_key(name, param, fixed)
    with open_store(db) as store:
        done: dict[str, str] = {} if redo else dict(store.cache_get(key) or {})
        todo = [i for i in items if i not in done]
        report.skipped = [i for i in items if i in done]

        def one(item: str) -> Any:
            return run(pipeline, vars=vars, params={**fixed, param: item}, db=db)

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            pending: dict[Future, str] = {}
            queue = list(todo)
            try:
                while queue or pending:
                    while queue and len(pending) < concurrency:
                        item = queue.pop(0)
                        pending[pool.submit(one, item)] = item
                    finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for fut in finished:
                        item = pending.pop(fut)
                        try:
                            result = fut.result()
                            err = None if result.status == "succeeded" else (result.error or result.status)
                            report.runs[item] = result.run_id
                        except Exception as e:
                            err = f"{type(e).__name__}: {e}"
                        if err is None:
                            report.succeeded.append(item)
                            done[item] = report.runs[item]
                            store.cache_set(key, done)  # after each run: an interruption loses nothing
                        else:
                            report.failed[item] = err
                        if on_done:
                            on_done(item, "succeeded" if err is None else "failed", err)
            except BaseException:
                for fut in pending:
                    fut.cancel()
                raise
    return report
