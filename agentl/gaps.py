"""Completeness: which partitions (days, hours, key x day) are missing in the destination.

A pipeline declares what it should have produced under a top-level `expect:` block:

    expect:
      partitions: { every: 1d, from: "2025-01-01", skip: weekends, field: ref_date }
      present_in: { use: sqlite.read, with: { path: out.db, query: "select distinct ref_date from facts" } }

`present_in` is any reader connector (rows whose `field` holds the partition value) or
`{files: "out/{date:%Y-%m-%d}/*.csv"}`, a glob formatted once per expected partition.
"""

from __future__ import annotations

import glob as _glob
import itertools
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SKIPS = {"weekends", "weekdays"}


class PartitionsSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    every: Literal["1d", "1h", "day", "hour"] = "1d"
    from_: str = Field(alias="from")
    to: str | None = None  # default: the last complete period before now (UTC)
    skip: str | list[str] | None = None  # weekends, weekdays or ISO dates
    field: str = "date"  # column / record key holding the partition value
    format: str | None = None  # default %Y-%m-%d (daily) or %Y-%m-%dT%H (hourly)
    keys: dict[str, list[str]] = Field(default_factory=dict)  # composite: source x date x part

    @property
    def hourly(self) -> bool:
        return self.every in ("1h", "hour")

    @property
    def fmt(self) -> str:
        return self.format or ("%Y-%m-%dT%H" if self.hourly else "%Y-%m-%d")


class PresentInSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    use: str | None = None
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    files: str | None = None

    @model_validator(mode="after")
    def _one(self) -> PresentInSpec:
        if bool(self.use) == bool(self.files):
            raise ValueError("present_in: set either `use:` (a reader) or `files:` (a glob)")
        return self


class ExpectSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    partitions: PartitionsSpec
    present_in: PresentInSpec


@dataclass
class Gaps:
    expected: int
    missing: list[dict[str, Any]] = field(default_factory=list)
    every: str = "1d"

    def ranges(self) -> list[tuple[dict[str, str], str, str]]:
        """Missing partitions as contiguous runs: (keys, first, last), one per key combination."""
        step = timedelta(hours=1) if self.every in ("1h", "hour") else timedelta(days=1)
        merged: list[tuple[dict[str, str], str, str]] = []
        last_at: datetime | None = None
        for p in self.missing:
            keys = {k: v for k, v in p.items() if k not in ("period", "_at")}
            at = p["_at"]
            if merged and merged[-1][0] == keys and last_at is not None and at - last_at == step:
                merged[-1] = (keys, merged[-1][1], p["period"])
            else:
                merged.append((keys, p["period"], p["period"]))
            last_at = at
        return merged

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected": self.expected,
            "missing": [{k: v for k, v in p.items() if k != "_at"} for p in self.missing],
            "ranges": [{**k, "from": a, "to": b} for k, a, b in self.ranges()],
        }


def _parse(value: str) -> datetime:
    v = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(v)
    return dt.replace(tzinfo=None) if dt.tzinfo is None else dt.astimezone(timezone.utc).replace(tzinfo=None)


def expected(p: PartitionsSpec, since: str | None = None, until: str | None = None) -> list[datetime]:
    """Every period from `from` (or `since`, if later) to `to` (default: the last complete one)."""
    step = timedelta(hours=1) if p.hourly else timedelta(days=1)
    start = _parse(p.from_)
    if since and _parse(since) > start:
        start = _parse(since)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    end_raw = until or p.to
    if end_raw:
        end = _parse(end_raw)
    else:
        end = now.replace(minute=0, second=0, microsecond=0) - step
        if not p.hourly:
            end = end.replace(hour=0)
    start = start.replace(minute=0, second=0, microsecond=0)
    if not p.hourly:
        start, end = start.replace(hour=0), end.replace(hour=0, minute=0, second=0, microsecond=0)
    skips = [p.skip] if isinstance(p.skip, str) else list(p.skip or [])
    days = {_parse(s).date() for s in skips if s not in _SKIPS}
    out: list[datetime] = []
    cur = start
    while cur <= end:
        wd = cur.weekday()
        if not (("weekends" in skips and wd >= 5) or ("weekdays" in skips and wd < 5) or cur.date() in days):
            out.append(cur)
        cur += step
    return out


def _norm(value: Any, p: PartitionsSpec) -> str:
    """A destination value (date, datetime, string, timestamp) as the partition's string."""
    if isinstance(value, datetime):
        return value.strftime(p.fmt)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day).strftime(p.fmt)
    s = str(value).strip()
    try:
        return _parse(s).strftime(p.fmt)
    except ValueError:
        return s


def present(spec: ExpectSpec, periods: list[datetime], base_dir: str | None = None) -> set[tuple[str, ...]]:
    """(period, *key values) tuples found in the destination."""
    p, src = spec.partitions, spec.present_in
    keys = list(p.keys)
    found: set[tuple[str, ...]] = set()
    if src.files:
        base = Path(base_dir or ".")
        combos = list(itertools.product(*(p.keys[k] for k in keys)))
        for at in periods:
            for combo in combos:
                pattern = src.files.format(date=at, **dict(zip(keys, combo, strict=True)))
                if not Path(pattern).is_absolute():
                    pattern = str(base / pattern)
                if _glob.glob(pattern):
                    found.add((at.strftime(p.fmt), *combo))
        return found
    from . import registry

    assert src.use is not None
    rows = registry.get(src.use)([], **src.with_)
    for row in rows:
        if p.field not in row or row[p.field] is None:
            continue
        found.add((_norm(row[p.field], p), *(str(row.get(k)) for k in keys)))
    return found


def find(
    spec: ExpectSpec, since: str | None = None, until: str | None = None, base_dir: str | None = None
) -> Gaps:
    p = spec.partitions
    periods = expected(p, since, until)
    have = present(spec, periods, base_dir)
    keys = list(p.keys)
    combos = list(itertools.product(*(p.keys[k] for k in keys)))
    missing: list[dict[str, Any]] = []
    for combo in combos:  # grouped by key combination, then time: contiguous runs read well
        for at in periods:
            label = at.strftime(p.fmt)
            if (label, *combo) not in have:
                missing.append({**dict(zip(keys, combo, strict=True)), "period": label, "_at": at})
    return Gaps(expected=len(periods) * len(combos), missing=missing, every=p.every)


def backfill_commands(pipeline: str, gaps: Gaps, fmt: str = "%Y-%m-%d") -> list[str]:
    """`agentl backfill` lines (#182) for daily gaps; hourly or keyed gaps need a param per run."""
    if gaps.every in ("1h", "hour") or fmt != "%Y-%m-%d":
        return []
    out = []
    for keys, first, last in gaps.ranges():
        extra = "".join(f" --param {k}={v}" for k, v in keys.items())
        out.append(f"agentl backfill {pipeline} --from {first} --to {last}{extra}")
    return out
