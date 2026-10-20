"""Run errors grouped by fingerprint (#356): alert once, count repeats, notice the fix.

A failed run's error is reduced to a fingerprint: the error class, the step that failed and the
message with numbers, ids, paths and timestamps stripped, so the same failure keeps one id across
runs. The store keeps one row per pipeline and fingerprint (first and last seen, count, open or
resolved, last alert). Notify rules with `dedupe: true` alert once per fingerprint and stay quiet
on repeats, or remind every `remind_every` with the count; the first successful run afterwards
emits `run.resolved`. `notify.digest` lists the errors in its `errors` section.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable

from .report import _error_class

now: Callable[[], float] = time.time  # tests swap this for a fake clock

_NORMALISE = [
    # ISO dates and times, with an optional zone: 2026-01-02T03:04:05.123Z, 2026-01-02 03:04
    (
        re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"),
        "<ts>",
    ),
    (re.compile(r"\b\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\b"), "<ts>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<id>"),
    (re.compile(r"\b(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{8,}\b"), "<id>"),  # hashes, run ids
    (re.compile(r"(?:[A-Za-z]:\\|\\\\|~?/|\./|\.\./)[^\s'\":,;)\]]*"), "<path>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
    (re.compile(r"\s+"), " "),
]


def normalise(message: str) -> str:
    """The message with timestamps, ids, paths and numbers replaced, so repeats compare equal."""
    for pattern, repl in _NORMALISE:
        message = pattern.sub(repl, message)
    return message.strip()


def split(error: str) -> tuple[str, str]:
    """(error class, message) of a `Class: message` error, as `agentl report` reads it."""
    cls = _error_class(error, None)
    rest = error.split(":", 1)[1] if ":" in error else error
    return cls, rest.strip()


def fingerprint(error: str, step: str | None) -> str:
    """A short id for one kind of run failure: error class + step + normalised message. The class
    is read like the `agentl report` fingerprint; the id has its own prefix."""
    cls, message = split(error)
    key = f"{cls}|{step or ''}|{normalise(message)}"
    return "agentl-err-" + hashlib.sha256(key.encode()).hexdigest()[:12]


def iso(ts: float) -> str:
    """A store timestamp as UTC ISO text, to the second."""
    import datetime as _dt

    return _dt.datetime.fromtimestamp(float(ts), _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def should_alert(row: dict, remind_every: float | None, at: float) -> bool:
    """A failure with this store row alerts when nothing was sent for it yet, or when the last
    alert is at least `remind_every` seconds old."""
    alerted = row.get("alerted")
    if alerted is None:
        return True
    return remind_every is not None and at - float(alerted) >= remind_every
