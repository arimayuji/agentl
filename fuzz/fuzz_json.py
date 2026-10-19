"""Fuzz the JSON and JSON Lines reader (agentl.connectors.files.read_json) on raw bytes."""

from __future__ import annotations

import sys

import atheris

with atheris.instrument_imports():
    from agentl.connectors import files

from common import write


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    suffix = fdp.PickValueInList([".json", ".jsonl"])
    encoding = fdp.PickValueInList(["utf-8", "auto"])
    path = write(fdp.ConsumeBytes(fdp.remaining_bytes()), suffix)
    try:
        files.read_json(path, encoding=encoding)
    except ValueError:
        pass  # invalid JSON or bytes that do not decode (both subclass ValueError)
    except RecursionError:
        pass  # deeply nested arrays


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
