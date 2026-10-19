"""Shared helpers for the fuzz targets: the readers take a path, so each input goes to a temp file."""

from __future__ import annotations

import tempfile
from pathlib import Path

_DIR = Path(tempfile.mkdtemp(prefix="agentl-fuzz-"))


def write(data: bytes, suffix: str) -> Path:
    """Write `data` to a reusable temp file with `suffix` and return its path."""
    p = _DIR / f"input{suffix}"
    p.write_bytes(data)
    return p


def offline() -> None:
    """Secret references (${aws-sm:...}) would call a cloud API: make them fail offline instead."""
    from agentl import secrets

    def refuse(text: str) -> str:
        raise RuntimeError("secret lookups are disabled while fuzzing")

    secrets.substitute = refuse
