"""Fuzz the pipeline YAML loader and spec validation (agentl.spec.load)."""

from __future__ import annotations

import sys

import atheris

with atheris.instrument_imports():
    from agentl import spec

from common import offline, write


def TestOneInput(data: bytes) -> None:
    path = write(data, ".yaml")
    try:
        spec.load(path, env={"HOME": "/tmp", "NAME": "x"})
    except spec.SpecError:
        pass  # the documented error for bad YAML or a bad pipeline
    except UnicodeDecodeError:
        pass  # pipeline files must be UTF-8
    except RecursionError:
        pass  # deeply nested YAML


def main() -> None:
    offline()
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
