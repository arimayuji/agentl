"""Fuzz the CSV reader (agentl.connectors.files.iter_csv) on raw bytes."""

from __future__ import annotations

import csv
import sys

import atheris

with atheris.instrument_imports():
    from agentl.connectors import files

from common import write


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    encoding = fdp.PickValueInList(["utf-8", "latin-1", "auto"])
    delimiter = fdp.PickValueInList([None, ",", ";", "\t", "|"])
    path = write(fdp.ConsumeBytes(fdp.remaining_bytes()), ".csv")
    try:
        for _ in files.iter_csv(path, delimiter=delimiter, encoding=encoding):
            pass
    except csv.Error:
        pass  # malformed CSV (e.g. a NUL byte or a field over the size limit)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
