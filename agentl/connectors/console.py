"""Record sinks for the terminal and the log: `console.send` and `log.write`.

Both pass their input through unchanged, so they can sit in the middle of a pipeline too."""

from __future__ import annotations

import logging
import sys
from typing import Any

from ..notify import format_records
from ..registry import Records, connector
from ._util import single

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


@connector("console.send")
def console_send(
    input: Any, template: str | None = None, max_rows: int | None = None, stream: str = "stdout"
) -> Records:
    """Print one line per record (`template`, or JSON) to stdout or stderr."""
    rows = single(input)
    out = sys.stderr if stream == "stderr" else sys.stdout
    for line in format_records(rows, template, max_rows):
        print(line, file=out)
    return rows


@connector("log.write")
def log_write(
    input: Any,
    template: str | None = None,
    max_rows: int | None = None,
    level: str = "info",
    logger: str = "agentl.records",
) -> Records:
    """Log one message per record through Python logging (`logger`, at `level`)."""
    if level.lower() not in _LEVELS:
        raise ValueError(f"log.write: unknown level {level!r}; use one of {sorted(_LEVELS)}")
    rows = single(input)
    log = logging.getLogger(logger)
    for line in format_records(rows, template, max_rows):
        log.log(_LEVELS[level.lower()], line)
    return rows
