"""Scheduled digest of the check rules' findings (#218): `notify.digest`.

Each run of a check step with per-row `rules:` keeps a daily (UTC) bucket of finding ids in the
store (see `Runner._run_rules`). This module reads those buckets once per period, at `at` in `tz`,
and sends what is new, resolved and ongoing to the digest's channels. A "sent today" guard per
channel, kept in the store, stops a retry or a second `agentl serve` tick from sending it twice.
On a clean day the heartbeat says "all clear", so a missing digest means the service did not run.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from . import notify
from .checks import digest as compare
from .checks import parse_duration
from .spec import DigestSpec, PipelineSpec

log = logging.getLogger("agentl.digest")
CAP = 50  # finding ids listed per section


def guard_key(pipeline: str, channel: str) -> str:
    return f"notify-digest:{pipeline}:{channel}"


def _bucket_key(pipeline: str, step: str) -> str:
    from .runner import _hash  # the key the runner writes the bucket under

    return _hash("check-digest", pipeline, step)


def period_day(d: DigestSpec, now: _dt.datetime) -> str | None:
    """The local day this period's digest belongs to, or None before `at` on that day."""
    from zoneinfo import ZoneInfo

    local = now.astimezone(ZoneInfo(d.tz))
    hh, mm = (int(x) for x in d.at.split(":"))
    if (local.hour, local.minute) < (hh, mm):
        return None
    return local.date().isoformat()


def _local_day(d: DigestSpec, now: _dt.datetime) -> str:
    from zoneinfo import ZoneInfo

    return now.astimezone(ZoneInfo(d.tz)).date().isoformat()


def due(d: DigestSpec, now: _dt.datetime, last_sent: str | None) -> str | None:
    """The day to send for, when a digest is due at `now` and the last one went out on
    `last_sent` (a local ISO day, or None); None when nothing is due."""
    day = period_day(d, now)
    if day is None:
        return None
    if last_sent is not None:
        every = int(parse_duration(d.every) // 86400)
        if (_dt.date.fromisoformat(day) - _dt.date.fromisoformat(last_sent)).days < every:
            return None
    return day


def collect(spec: PipelineSpec, store: Any, today: str) -> list[dict[str, Any]]:
    """Per check step with row rules: the comparison of its bucket with the day before. `today`
    is the UTC day of the buckets; a step whose last bucket is older did not run today."""
    out = []
    for step in spec.steps:
        if not (step.check and step.rules):
            continue
        bucket = store.cache_get(_bucket_key(spec.name, step.id)) or {}
        ran = bucket.get("day") == today
        ids, prev = (bucket.get("ids", []), bucket.get("prev", [])) if ran else ([], bucket.get("ids", []))
        out.append({"step": step.id, "ran": ran, "last": bucket.get("day"), **compare(ids, prev)})
    return out


def render(spec: PipelineSpec, d: DigestSpec, entries: list[dict[str, Any]], day: str) -> tuple[str, bool]:
    """(text, clean). Clean: every step ran today and none has findings now or yesterday."""
    lines = [f"[agentl] {spec.name}: digest for {day}"]
    clean = True
    for e in entries:
        if not e["ran"]:
            clean = False
            last = f"last on {e['last']}" if e["last"] else "never ran"
            lines.append(f"{e['step']}: no run today ({last})")
            continue
        counts = {name: len(e[name]) for name in d.sections}
        if not any(e[name] for name in ("new", "resolved", "ongoing")):
            lines.append(f"{e['step']}: all clear")
            continue
        clean = clean and not any(counts.values())
        lines.append(f"{e['step']}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        for name in d.sections:
            items = e[name]
            if items:
                lines.append(f"  {name}:")
                lines += [f"    - {i}" for i in items[:CAP]]
                if len(items) > CAP:
                    lines.append(f"    ... {len(items) - CAP} more")
    if not entries:
        lines.append("no check step with rules: in this pipeline")
    elif clean:
        lines.append("all clear: no findings today")
    return "\n".join(lines), clean


def send_due(
    spec: PipelineSpec, store: Any, now: _dt.datetime | None = None, force: bool = False
) -> dict[str, str]:
    """Send the digest to each channel it is due on. Returns {channel: outcome}, where outcome
    is `sent`, `already sent`, `not due`, `all clear (no heartbeat)` or `failed: <error>`.
    `force` sends even when not due or already sent (the guard is still updated)."""
    if spec.notify is None or spec.notify.digest is None:
        return {}
    d, channels = spec.notify.digest, spec.notify.channels
    now = now or _dt.datetime.now(_dt.timezone.utc)
    utc_day = now.astimezone(_dt.timezone.utc).date().isoformat()
    text: str | None = None
    clean = False
    out: dict[str, str] = {}
    for name in d.to:
        key = guard_key(spec.name, name)
        last = store.cache_get(key)
        day = _local_day(d, now) if force else due(d, now, last)
        if day is None:
            sent_this_period = last is not None and period_day(d, now) is not None
            out[name] = "already sent" if sent_this_period else "not due"
            continue
        if text is None:
            text, clean = render(spec, d, collect(spec, store, utc_day), day)
        if clean and not d.heartbeat:
            out[name] = "all clear (no heartbeat)"
            continue
        event = {
            "type": "check.digest.daily",
            "pipeline": spec.name,
            "step": None,
            "data": {"day": day, "all_clear": clean},
        }
        if not d.idempotent:
            store.cache_set(key, day)  # at most once: the guard goes first
        try:
            notify.send(name, channels[name], text, event)
        except Exception as e:  # a broken channel must never fail the caller
            log.warning("digest to %s failed: %s", name, e)
            out[name] = f"failed: {e}"
            continue
        if d.idempotent:
            store.cache_set(key, day)  # written only after a successful send
        out[name] = "sent"
    return out
