"""`time.normalize`: turn interval labels (hour ending, hour beginning, accumulated windows) into
instants, with daylight saving gaps and overlaps handled explicitly (#213)."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from ..registry import connector
from ._util import single

KINDS = ("hour_ending", "hour_beginning", "accumulated")
CONVENTIONS = ("interval_start", "interval_end")
UTC = dt.timezone.utc
_CLOCK = re.compile(r"(\d{1,2})[:h](\d{2})(?::(\d{2}))?")


class _Skip(ValueError):
    """A record the policy sends to the rejected records."""


def _zone(name: str | None) -> dt.tzinfo:
    if not name or name.upper() in ("UTC", "Z"):
        return UTC
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)


def _clock(s: str) -> dt.timedelta | None:
    """`13:30` or `24:00` as an offset from midnight; None when `s` is not a clock time."""
    m = _CLOCK.fullmatch(s.strip())
    if not m:
        return None
    h, mi, se = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if mi > 59 or se > 59 or h > 24 or (h == 24 and (mi or se)):
        raise ValueError(f"{s!r}: not a time of day (00:00 to 24:00)")
    return dt.timedelta(hours=h, minutes=mi, seconds=se)


def _split(v: Any) -> tuple[Any, Any]:
    """`2026-03-08 24:00` -> (date part, clock part); the clock part may be 24:00."""
    s = str(v).strip()
    m = re.fullmatch(r"(.+?)[ T](\d{1,2}[:h]\d{2}(?::\d{2})?)", s)
    if not m:
        return s, None
    return m.group(1), m.group(2)


def _resolve(wall: dt.datetime, zone: dt.tzinfo, on_missing: str, on_ambiguous: str) -> dt.datetime:
    """A naive local wall time as a UTC instant, refusing DST gaps and overlaps unless told."""
    if zone is UTC:
        return wall.replace(tzinfo=UTC)
    a = wall.replace(tzinfo=zone, fold=0)
    b = wall.replace(tzinfo=zone, fold=1)
    if a.utcoffset() == b.utcoffset():
        return a.astimezone(UTC)
    back = a.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if back != wall:  # the clock jumped over this time
        if on_missing == "fail":
            raise ValueError(f"{wall.isoformat()} does not exist in {zone} (DST gap)")
        if on_missing == "reject":
            raise _Skip(f"{wall.isoformat()} does not exist in {zone} (DST gap)")
        # shift: the wall time read with the offset before the jump, i.e. moved forward
        return a.astimezone(UTC)
    if on_ambiguous == "fail":
        raise ValueError(f"{wall.isoformat()} happens twice in {zone} (DST overlap)")
    if on_ambiguous == "reject":
        raise _Skip(f"{wall.isoformat()} happens twice in {zone} (DST overlap)")
    return (a if on_ambiguous == "first" else b).astimezone(UTC)


@connector("time.normalize")
def normalize(
    input: Any,
    to: dict[str, Any],
    **params: Any,
) -> list[dict]:
    """Interval labels to instants (#213). `from`: `column` (the label), `kind` (hour_ending,
    hour_beginning or accumulated), `date_column` (else the label holds the date too, as
    `2026-03-08 24:00`), `tz` (IANA zone of the labels), `base` (first label number: 1 for
    hour_ending, 0 otherwise), `interval` (minutes per label, 60; 30 for half hours), `clock`
    (wall: labels are local clock times; elapsed: labels count real time since local midnight,
    so a 25-hour day has labels up to 25), `since` (accumulated: window start, `00:00`). Labels
    are numbers (fractions allowed) or clock times (`13:30`, `24:00`). `to`: `column`,
    `convention` (interval_start or interval_end), `tz` (UTC), `end_column` (optional, the
    other edge). `on_missing` (fail, reject, shift) and `on_ambiguous` (fail, reject, first,
    last) decide DST gaps and overlaps; both fail by default."""
    from .. import types
    from ..registry import Rejects

    src = dict(params.pop("from", None) or {})
    on_missing = params.pop("on_missing", "fail")
    on_ambiguous = params.pop("on_ambiguous", "fail")
    if params:
        raise ValueError(f"time.normalize: unknown option(s) {sorted(params)}")
    if on_missing not in ("fail", "reject", "shift"):
        raise ValueError("time.normalize: on_missing is fail, reject or shift")
    if on_ambiguous not in ("fail", "reject", "first", "last"):
        raise ValueError("time.normalize: on_ambiguous is fail, reject, first or last")
    column = src.get("column")
    if not column:
        raise ValueError("time.normalize: from.column is required")
    kind = src.get("kind", "hour_ending")
    if kind not in KINDS:
        raise ValueError(f"time.normalize: from.kind is one of {', '.join(KINDS)}")
    clock_mode = src.get("clock", "wall")
    if clock_mode not in ("wall", "elapsed"):
        raise ValueError("time.normalize: from.clock is wall or elapsed")
    base = float(src.get("base", 1 if kind == "hour_ending" else 0))
    step = dt.timedelta(minutes=float(src.get("interval", 60)))
    if step <= dt.timedelta(0):
        raise ValueError("time.normalize: from.interval must be positive")
    since = _clock(str(src.get("since", "00:00")))
    if since is None:
        raise ValueError("time.normalize: from.since is a time such as 00:00")
    date_column = src.get("date_column")
    zone = _zone(src.get("tz"))
    out_col = to.get("column")
    if not out_col:
        raise ValueError("time.normalize: to.column is required")
    convention = to.get("convention", "interval_start")
    if convention not in CONVENTIONS:
        raise ValueError("time.normalize: to.convention is interval_start or interval_end")
    out_zone = _zone(to.get("tz", "UTC"))
    end_col = to.get("end_column")

    def place(day: dt.date, offset: dt.timedelta) -> dt.datetime:
        midnight = dt.datetime(day.year, day.month, day.day)
        if clock_mode == "elapsed":
            start = _resolve(midnight, zone, "shift", "first")
            return start + offset
        return _resolve(midnight + offset, zone, on_missing, on_ambiguous)

    def edges(r: dict) -> tuple[dt.datetime, dt.datetime]:
        label = r.get(column)
        if date_column:
            day_v, lab = r.get(date_column), label
        else:
            day_v, lab = _split(label)
            if lab is None:
                raise ValueError(f"{label!r}: no time in the label (or set from.date_column)")
        moment = types.to_datetime(day_v)
        if moment is None or lab is None or str(lab).strip() == "":
            raise ValueError("empty date or label")
        day = moment.date()
        offset = _clock(lab) if isinstance(lab, str) else None
        if offset is None:
            n = float(lab)
            offset = (n - base) * step
            if kind == "hour_ending":
                offset += step
            if offset < dt.timedelta(0):
                raise ValueError(f"label {lab!r} is before the first one ({base:g})")
        if clock_mode == "elapsed":
            nxt = day + dt.timedelta(days=1)
            length = place(nxt, dt.timedelta(0)) - place(day, dt.timedelta(0))
            if offset > length:
                raise ValueError(f"label {lab!r} is past the end of a {length} day")
        point = place(day, offset)
        if kind == "hour_ending":
            return point - step, point
        if kind == "hour_beginning":
            return point, point + step
        start = place(day, since)
        if point < start:
            raise ValueError(f"label {lab!r} ends before the window start {src.get('since')}")
        return start, point

    good: list[dict] = []
    bad: list[dict] = []
    for r in single(input):
        try:
            start, end = edges(r)
        except _Skip as e:
            bad.append({**r, "_error": str(e), "_raw": {column: r.get(column)}})
            continue
        except (ValueError, TypeError, OverflowError) as e:
            raise ValueError(f"time.normalize: {column}={r.get(column)!r}: {e}") from None
        first, other = (start, end) if convention == "interval_start" else (end, start)
        out = dict(r)
        out[out_col] = first.astimezone(out_zone).isoformat()
        if end_col:
            out[end_col] = other.astimezone(out_zone).isoformat()
        good.append(out)
    return Rejects(good, bad)
