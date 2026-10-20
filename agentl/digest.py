"""Scheduled digest of the check rules' findings (#218): `notify.digest`.

Each run of a check step with per-row `rules:` keeps a daily (UTC) bucket of finding ids in the
store (see `Runner._run_rules`). This module reads those buckets once per period, at `at` in `tz`,
and sends what is new, resolved and ongoing to the digest's channels. A "sent today" guard per
channel, kept in the store, stops a retry or a second `agentl serve` tick from sending it twice.
On a clean day the heartbeat says "all clear", so a missing digest means the service did not run.
The `errors` section (#356) lists the run errors kept by fingerprint (agentl/errors.py).
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


def guard_key(pipeline: str, channel: str, errors_only: bool = False) -> str:
    kind = "notify-digest-errors" if errors_only else "notify-digest"  # its own guard (#356)
    return f"{kind}:{pipeline}:{channel}"


def errors_only(d: DigestSpec) -> DigestSpec:
    """The digest with only its `errors` section, for `agentl digest --errors`."""
    return d.model_copy(update={"sections": ["errors"]})


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


CHECK_SECTIONS = ("new", "resolved", "ongoing")


def collect_errors(spec: PipelineSpec, d: DigestSpec, store: Any, now: _dt.datetime) -> list[dict[str, Any]]:
    """The pipeline's open run errors and those resolved within the digest period (#356)."""
    since = now.timestamp() - parse_duration(d.every)
    return store.errors_of(spec.name, resolved_since=since)


def render_errors(rows: list[dict[str, Any]]) -> list[str]:
    """The `errors` section: one line per error, open ones first."""
    from .errors import iso

    open_ = [r for r in rows if r["status"] == "open"]
    fixed = [r for r in rows if r["status"] != "open"]
    lines = [f"errors: open {len(open_)}, resolved {len(fixed)}"]
    for title, group in (("open", open_), ("resolved", fixed)):
        if not group:
            continue
        lines.append(f"  {title}:")
        for r in group[:CAP]:
            where = f"step {r['step']}" if r["step"] else "run"
            seen = f"first {iso(r['first_seen'])}, last {iso(r['last_seen'])}"
            if r["status"] != "open":
                seen += f", resolved {iso(r['resolved'])}"
            lines.append(f"    - {_error_line(r)} ({where}) x{r['count']}, {seen}")
        if len(group) > CAP:
            lines.append(f"    ... {len(group) - CAP} more")
    return lines


def _error_line(r: dict[str, Any]) -> str:
    error = str(r["error"] or "").splitlines()[0] if r["error"] else r["error_class"]
    return f"{r['pipeline']}: {error[:200]} [{r['fingerprint']}]"


def render(
    spec: PipelineSpec,
    d: DigestSpec,
    entries: list[dict[str, Any]],
    day: str,
    errors: list[dict[str, Any]] | None = None,
) -> tuple[str, bool]:
    """(text, clean). Clean: every step ran today and none has findings now or yesterday, and,
    with the `errors` section, no run error is open or was resolved in the period."""
    lines = [f"[agentl] {spec.name}: digest for {day}"]
    clean = True
    checks = [name for name in d.sections if name in CHECK_SECTIONS]
    if not checks:
        entries = []
    for e in entries:
        if not e["ran"]:
            clean = False
            last = f"last on {e['last']}" if e["last"] else "never ran"
            lines.append(f"{e['step']}: no run today ({last})")
            continue
        counts = {name: len(e[name]) for name in checks}
        if not any(e[name] for name in ("new", "resolved", "ongoing")):
            lines.append(f"{e['step']}: all clear")
            continue
        clean = clean and not any(counts.values())
        lines.append(f"{e['step']}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        for name in checks:
            items = e[name]
            if items:
                lines.append(f"  {name}:")
                lines += [f"    - {i}" for i in items[:CAP]]
                if len(items) > CAP:
                    lines.append(f"    ... {len(items) - CAP} more")
    if checks and not entries:
        lines.append("no check step with rules: in this pipeline")
    if "errors" in d.sections:
        rows = errors or []
        clean = clean and not rows
        lines += render_errors(rows) if rows else ["errors: none open"]
    if clean and (entries or "errors" in d.sections):
        lines.append("all clear: no findings today" if checks else "all clear: no run errors")
    return "\n".join(lines), clean


def show(spec: PipelineSpec, store: Any, now: _dt.datetime | None = None, only_errors: bool = False) -> str:
    """The digest's text as it would be sent now (for `agentl digest --show`)."""
    assert spec.notify is not None and spec.notify.digest is not None
    d = errors_only(spec.notify.digest) if only_errors else spec.notify.digest
    now = now or _dt.datetime.now(_dt.timezone.utc)
    utc_day = now.astimezone(_dt.timezone.utc).date().isoformat()
    rows = collect_errors(spec, d, store, now) if "errors" in d.sections else None
    return render(spec, d, collect(spec, store, utc_day), _local_day(d, now), rows)[0]


def send_due(
    spec: PipelineSpec,
    store: Any,
    now: _dt.datetime | None = None,
    force: bool = False,
    only_errors: bool = False,
) -> dict[str, str]:
    """Send the digest to each channel it is due on. Returns {channel: outcome}, where outcome
    is `sent`, `already sent`, `not due`, `all clear (no heartbeat)` or `failed: <error>`.
    `force` sends even when not due or already sent (the guard is still updated). `only_errors`
    sends just the `errors` section, under a guard of its own."""
    if spec.notify is None or spec.notify.digest is None:
        return {}
    d, channels = spec.notify.digest, spec.notify.channels
    if only_errors:
        d = errors_only(d)
    now = now or _dt.datetime.now(_dt.timezone.utc)
    utc_day = now.astimezone(_dt.timezone.utc).date().isoformat()
    text: str | None = None
    clean = False
    out: dict[str, str] = {}
    for name in d.to:
        key = guard_key(spec.name, name, only_errors)
        last = store.cache_get(key)
        day = _local_day(d, now) if force else due(d, now, last)
        if day is None:
            sent_this_period = last is not None and period_day(d, now) is not None
            out[name] = "already sent" if sent_this_period else "not due"
            continue
        if text is None:
            rows = collect_errors(spec, d, store, now) if "errors" in d.sections else None
            text, clean = render(spec, d, collect(spec, store, utc_day), day, rows)
        if clean and not d.heartbeat:
            out[name] = "all clear (no heartbeat)"
            continue
        event = {
            "type": "errors.digest.daily" if only_errors else "check.digest.daily",
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
