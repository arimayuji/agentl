from __future__ import annotations

import glob as _glob
from pathlib import Path
from typing import Any


def paths(input: Any, path: str | None, field: str = "path") -> list[Path]:
    """Files from a glob `path`, or from the `field` of upstream records."""
    if path:
        found = sorted(_glob.glob(path, recursive=True))
        if not found and not any(c in path for c in "*?["):
            raise FileNotFoundError(path)
        return [Path(p) for p in found]
    if isinstance(input, list):
        return [Path(r[field]) for r in input if r.get(field)]
    raise ValueError("set 'path' or feed records that have a 'path' field")


def single(input: Any) -> list[dict]:
    if input is None:
        return []
    if isinstance(input, dict):
        raise ValueError("this connector takes a single input step")
    return input
