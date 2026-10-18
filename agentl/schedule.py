"""Schedules by publication cycle: the days data is expected, holidays, staged polling.

    trigger:
      - schedule:
          tz: America/New_York                   # required
          expect: { days: [mon, tue], at: "16:00", calendar: us_federal, if_holiday: next_business_day }
          poll:
            cadence: [{ for: 15m, every: 1m }, { for: 45m, every: 5m }]
            stop_when: success_in_cycle
          active: { between: ["2026-09-01", "2026-11-30"] }
          extra_dates: [2026-02-19]

A cycle is one expected publication: a date and the time it is due. Each cycle fires at `at` and
then on the poll cadence until its window ends. Standard library only."""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


# ---------------------------------------------------------------- holidays
def _easter(year: int) -> _dt.date:
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month = (h + m - 7 * n + 114) // 31
    day = (h + m - 7 * n + 114) % 31 + 1
    return _dt.date(year, month, day)


def _nth(year: int, month: int, weekday: int, n: int) -> _dt.date:
    """n-th weekday (0 = Monday) of a month; n = -1 is the last one."""
    if n > 0:
        first = _dt.date(year, month, 1)
        return first + _dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    nxt = _dt.date(year + month // 12, month % 12 + 1, 1)
    last = nxt - _dt.timedelta(days=1)
    return last - _dt.timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: _dt.date) -> _dt.date:
    """US rule: a Saturday holiday is observed on Friday, a Sunday one on Monday."""
    if d.weekday() == 5:
        return d - _dt.timedelta(days=1)
    if d.weekday() == 6:
        return d + _dt.timedelta(days=1)
    return d


def _us_federal(year: int) -> set[_dt.date]:
    fixed = [(1, 1), (7, 4), (11, 11), (12, 25)] + ([(6, 19)] if year >= 2021 else [])
    out = {_observed(_dt.date(year, m, d)) for m, d in fixed}
    out |= {
        _nth(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth(year, 2, 0, 3),  # Washington's Birthday
        _nth(year, 5, 0, -1),  # Memorial Day
        _nth(year, 9, 0, 1),  # Labor Day
        _nth(year, 10, 0, 2),  # Columbus Day
        _nth(year, 11, 3, 4),  # Thanksgiving
    }
    return out


def _br_national(year: int) -> set[_dt.date]:
    fixed = [(1, 1), (4, 21), (5, 1), (9, 7), (10, 12), (11, 2), (11, 15), (12, 25)]
    if year >= 2024:
        fixed.append((11, 20))
    out = {_dt.date(year, m, d) for m, d in fixed}
    out.add(_easter(year) - _dt.timedelta(days=2))  # Good Friday
    return out


CALENDARS = {"us_federal": _us_federal, "br_national": _br_national}


def read_exceptions(path: str | Path) -> set[_dt.date]:
    """One ISO date per line (YYYY-MM-DD); blank lines and `#` comments are ignored."""
    out: set[_dt.date] = set()
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        text = line.split("#", 1)[0].strip()
        if text:
            try:
                out.add(_dt.date.fromisoformat(text))
            except ValueError as e:
                raise ValueError(f"{path}:{n}: '{text}' is not a date (YYYY-MM-DD)") from e
    return out


# ---------------------------------------------------------------- cycles
class Schedule:
    """Cycles and firings of a `schedule:` trigger (a `ScheduleSpec`)."""

    def __init__(self, spec: Any, base_dir: str | Path | None = None):
        from zoneinfo import ZoneInfo

        from .checks import parse_duration

        self.spec = spec
        self.tz = ZoneInfo(spec.tz)
        exp = spec.expect
        self.days = {_DAYS[d] for d in exp.days} if exp else set()
        self.at = _dt.time.fromisoformat(exp.at) if exp else _dt.time(0, 0)
        self.calendar = CALENDARS[exp.calendar] if exp and exp.calendar else None
        self.if_holiday = exp.if_holiday if exp else "skip"
        self.exceptions: set[_dt.date] = set()
        if exp and exp.exceptions:
            p = Path(exp.exceptions)
            self.exceptions = read_exceptions(p if p.is_absolute() or not base_dir else Path(base_dir) / p)
        self.extra = set(spec.extra_dates)
        self.skip = set(spec.skip_dates)
        self.between = spec.active.between if spec.active else None
        self.stages: list[tuple[float, float]] = []
        if spec.poll:
            self.stages = [(parse_duration(c.for_), parse_duration(c.every)) for c in spec.poll.cadence]
        self.window = _dt.timedelta(seconds=sum(f for f, _ in self.stages))
        self._years: dict[int, set[_dt.date]] = {}

    def holiday(self, d: _dt.date) -> bool:
        if d in self.exceptions:
            return True
        if self.calendar is None:
            return False
        if d.year not in self._years:
            self._years[d.year] = self.calendar(d.year)
        return d in self._years[d.year]

    def business_day(self, d: _dt.date) -> bool:
        return d.weekday() < 5 and not self.holiday(d)

    def _active(self, d: _dt.date) -> bool:
        return self.between is None or self.between[0] <= d <= self.between[1]

    def cycle_dates(self, start: _dt.date) -> Iterator[_dt.date]:
        """Cycle dates from `start` on, ascending, after holidays move or drop them."""
        found: set[_dt.date] = set()
        d = start - _dt.timedelta(days=14)  # a holiday shift can carry a cycle into `start`
        last = start + _dt.timedelta(days=366 * 3)
        while d <= last:
            if d in self.extra:
                found.add(d)
            elif d.weekday() in self.days and d not in self.skip and self._active(d):
                moved = self._shift(d) if self.holiday(d) else d
                if moved is not None:
                    found.add(moved)
            d += _dt.timedelta(days=1)
        yield from sorted(x for x in found if x >= start)

    def _shift(self, d: _dt.date) -> _dt.date | None:
        mode = self.if_holiday
        if mode == "run":
            return d
        if mode == "skip":
            return None
        step = _dt.timedelta(days=1 if mode == "next_business_day" else -1)
        x = d + step
        while not self.business_day(x):
            x += step
        return x

    def start_of(self, d: _dt.date) -> _dt.datetime:
        wall = _dt.datetime.combine(d, self.at, tzinfo=self.tz)
        return wall.astimezone(_dt.timezone.utc).astimezone(self.tz)  # normalize a DST gap

    def firings(self, d: _dt.date) -> list[_dt.datetime]:
        """The times one cycle fires: its start, then each poll stage at its pace."""
        start = self.start_of(d)
        utc = start.astimezone(_dt.timezone.utc)
        out, offset = [utc], 0.0
        for length, every in self.stages:
            t = offset + every
            while t <= offset + length + 1e-9:
                out.append(utc + _dt.timedelta(seconds=t))
                t += every
            offset += length
        return sorted({t.astimezone(self.tz) for t in out})

    def window_end(self, d: _dt.date) -> _dt.datetime:
        return self.start_of(d) + self.window

    def next_firings(self, after: _dt.datetime, n: int = 10) -> list[tuple[_dt.date, _dt.datetime]]:
        """The next `n` (cycle date, time) firings strictly after `after`."""
        out: list[tuple[_dt.date, _dt.datetime]] = []
        begin = after.astimezone(self.tz).date() - _dt.timedelta(days=int(self.window.days) + 1)
        for d in self.cycle_dates(begin):
            for t in self.firings(d):
                if t > after:
                    out.append((d, t))
                    if len(out) >= n:
                        return out
        return out
