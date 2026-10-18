"""Fetch windows: split a date range into chunks, run each one, keep a status per chunk.

A normal run covers `[today - lookback, today + 1d)` so recent revisions are re-read every time;
`agentl backfill --from X --to Y` covers `[X, Y)` through the same chunked path. Each chunk's
status is kept in the run store; a backfill that is resumed skips the chunks that succeeded."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from .spec import PipelineSpec, WindowSpec

_DURATION = re.compile(r"^\s*(\d+)\s*([dw])\s*$")
_PLACEHOLDER = re.compile(r"\$\{window\.(start|end)\}")
_STATE_STEP = "__window__"


def days(text: str) -> int:
    """`10d` -> 10, `2w` -> 14. Only days and weeks: windows are whole dates."""
    m = _DURATION.match(str(text))
    if not m or int(m.group(1)) < 1:
        raise ValueError(f"window: {text!r} is not a duration; use e.g. 10d or 2w")
    return int(m.group(1)) * (7 if m.group(2) == "w" else 1)


def _next_boundary(d: dt.date, align: str) -> dt.date | None:
    if align == "calendar_month":
        return dt.date(d.year + d.month // 12, d.month % 12 + 1, 1)
    if align == "week":
        return d + dt.timedelta(days=7 - d.weekday())
    return None


def plan(start: dt.date, end: dt.date, window: WindowSpec) -> list[tuple[dt.date, dt.date]]:
    """Chunks `(start, end)` covering `[start, end)`, end exclusive, in `window.order`."""
    if end <= start:
        raise ValueError(f"window: end {end} must be after start {start}")
    span = days(window.max_span) if window.max_span else None
    chunks: list[tuple[dt.date, dt.date]] = []
    cur = start
    while cur < end:
        stop = end
        boundary = _next_boundary(cur, window.align)
        if boundary is not None:
            stop = min(stop, boundary)
        if span is not None:
            stop = min(stop, cur + dt.timedelta(days=span))
        chunks.append((cur, stop))
        cur = stop
    return chunks[::-1] if window.order == "newest_first" else chunks


def _fill(value: Any, start: str, end: str) -> Any:
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda m: start if m.group(1) == "start" else end, value)
    if isinstance(value, list):
        return [_fill(v, start, end) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, start, end) for k, v in value.items()}
    return value


def bind(spec: PipelineSpec, start: dt.date, end: dt.date) -> PipelineSpec:
    """A copy of `spec` with `${window.start}` and `${window.end}` replaced in every `with:`."""
    a, b = start.isoformat(), end.isoformat()
    steps = [s.model_copy(update={"with_": _fill(s.with_, a, b)}) for s in spec.steps]
    return spec.model_copy(update={"steps": steps})


@dataclass
class WindowReport:
    status: str  # succeeded when every chunk succeeded
    done: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # already succeeded in an earlier backfill
    failed: dict[str, str] = field(default_factory=dict)  # chunk -> error
    results: dict[str, Any] = field(default_factory=dict)  # chunk -> RunResult

    @property
    def pending(self) -> list[str]:
        return list(self.failed)


def _key(start: dt.date, end: dt.date) -> str:
    return f"{start.isoformat()}/{end.isoformat()}"


def run_windows(
    spec: PipelineSpec,
    store: Any,
    start: dt.date | None = None,
    end: dt.date | None = None,
    *,
    resume: bool = True,
    today: dt.date | None = None,
    **runner_kw: Any,
) -> WindowReport:
    """Run `spec` once per chunk. Without `start`/`end`, covers the lookback tail ending today
    (always re-run, never skipped). With them (a backfill), chunks that succeeded before are
    skipped when `resume`. A failed chunk does not stop the others."""
    from .runner import Runner

    window = spec.window or WindowSpec()
    backfill = start is not None or end is not None
    if not backfill:
        today = today or dt.date.today()
        end = today + dt.timedelta(days=1)
        start = end - dt.timedelta(days=days(window.lookback))
    if start is None or end is None:
        raise ValueError("backfill needs both a start and an end")
    status: dict = (store.state_get(spec.name, _STATE_STEP) or {}) if store is not None else {}
    report = WindowReport(status="succeeded")
    for a, b in plan(start, end, window):
        key = _key(a, b)
        if backfill and resume and status.get(key, {}).get("status") == "succeeded":
            report.skipped.append(key)
            continue
        result = Runner(bind(spec, a, b), store=store, **runner_kw).run()
        report.results[key] = result
        if result.status == "succeeded":
            report.done.append(key)
        else:
            report.failed[key] = result.error or result.status
            report.status = "failed"
        if store is not None:
            status[key] = {"status": result.status, "run_id": result.run_id}
            store.state_set(spec.name, _STATE_STEP, status, result.run_id)
    return report
