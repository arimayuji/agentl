"""`skip_seen`: let through only the files or records no earlier successful run handled."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..registry import after_run, connector, seen_keys
from ._util import single


def _sha256(rec: dict) -> str:
    for field in ("content", "bytes", "data"):
        value = rec.get(field)
        if isinstance(value, (bytes, bytearray)):
            return hashlib.sha256(value).hexdigest()
    path = rec.get("path")
    if not path:
        raise ValueError("skip_seen key=sha256 needs a 'path' field (a local file) or bytes in 'content'")
    digest = hashlib.sha256()
    with Path(str(path)).open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _key(rec: dict, key: str) -> str:
    if key == "sha256":
        return _sha256(rec)
    if key not in rec or rec[key] is None:
        raise ValueError(f"skip_seen: record has no '{key}' field (fields: {', '.join(rec)})")
    return str(rec[key])


@connector("skip_seen")
def skip_seen(input: Any, key: str = "sha256", scope: str | None = None) -> list[dict]:
    """Drop records whose key an earlier successful run already let through. `key` is `sha256`
    (content of the file at `path`), `path`, or any field. Keys are kept in the run store under
    `scope` (default: the pipeline name) and marked only when the whole run succeeds."""
    rows = single(input)
    keys = [_key(r, key) for r in rows]
    store = seen_keys()
    if store is None:  # called directly: nothing is remembered
        return rows
    where = scope or store.default_scope
    known = store.filter(where, sorted(set(keys)))
    out, new = [], []
    for rec, k in zip(rows, keys, strict=True):
        if k in known:
            continue
        known.add(k)  # the same file twice in one run goes through once
        out.append(rec)
        new.append(k)
    if new:
        after_run(lambda: store.add(where, new))
    return out
