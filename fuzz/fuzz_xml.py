"""Fuzz the XML reader (agentl.connectors.files.read_xml) and the HTML tree parser."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

import atheris

with atheris.instrument_imports():
    from agentl.connectors import files, htmldom

from common import write


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    items = fdp.PickValueInList([None, "item", "row"])
    raw = fdp.ConsumeBytes(fdp.remaining_bytes())
    try:
        files.read_xml(write(raw, ".xml"), items=items)
    except ET.ParseError:
        pass  # malformed XML
    except ValueError:
        pass  # entity declarations are refused on purpose
    htmldom.parse(raw.decode("utf-8", errors="replace"))


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
