"""Fuzz the small text parsers: ${VAR} interpolation, the {field} template fill, wait_until
conditions, durations and percentages."""

from __future__ import annotations

import contextlib
import sys

import atheris

with atheris.instrument_imports():
    from agentl import checks, spec, waits
    from agentl.connectors import chat

from common import offline


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    text = fdp.ConsumeUnicodeNoSurrogates(256)
    key = fdp.ConsumeUnicodeNoSurrogates(16)
    value = fdp.ConsumeUnicodeNoSurrogates(64)
    rest = fdp.ConsumeUnicodeNoSurrogates(256)

    with contextlib.suppress(spec.SpecError):  # an unset ${VAR} without a default
        spec.interpolate({"a": [text, {"b": rest}]}, {key: value})

    out = chat.fill(text, {key: value, "n": 1, "none": None})
    assert isinstance(out, str)

    for parse in (waits.parse_condition, checks.parse_duration, checks.parse_percent):
        with contextlib.suppress(ValueError):  # the documented error for an invalid value
            parse(rest)


def main() -> None:
    offline()
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
