"""Snapshots of step outputs (#211): compare records by key, not by order, with a per-field diff.

Also holds the redaction used for anything recorded to disk (secrets in headers and URLs).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "***"

SECRET_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
    "x-auth-token",
}
_SECRET_NAME = re.compile(r"(token|secret|password|passwd|api[_-]?key|apikey|signature|auth|session)", re.I)


def redact_headers(headers: Mapping[str, Any]) -> dict[str, Any]:
    """Headers with credentials replaced by `***` (authorization, cookies, api keys, tokens)."""
    out: dict[str, Any] = {}
    for k, v in headers.items():
        secret = k.lower() in SECRET_HEADERS or bool(_SECRET_NAME.search(k))
        out[k] = REDACTED if secret else v
    return out


def redact_url(url: str) -> str:
    """URL with the password and secret-looking query parameters replaced by `***`."""
    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        cred, host = netloc.rsplit("@", 1)
        user = cred.split(":", 1)[0]
        netloc = f"{user}:{REDACTED}@{host}" if ":" in cred else f"{REDACTED}@{host}"
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        query = urlencode([(k, REDACTED if _SECRET_NAME.search(k) else v) for k, v in pairs], safe="*")
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _keyed(records: Iterable[Mapping[str, Any]], key: list[str] | None) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in records:
        rec = json.loads(_canon(dict(r)))
        k = _canon([rec.get(f) for f in key]) if key else _canon(rec)
        out.setdefault(k, []).append(rec)
    return out


@dataclass
class Diff:
    """Changes between a snapshot and new records, matched by key."""

    added: list[dict] = field(default_factory=list)
    removed: list[dict] = field(default_factory=list)
    changed: list[tuple[str, dict[str, tuple[Any, Any]]]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def render(self, label: str = "") -> str:
        head = f"{label}: " if label else ""
        lines: list[str] = []
        for key, fields in self.changed:
            lines.append(f"{head}~ {key}")
            for f, (old, new) in fields.items():
                lines.append(f"    {f}: {_canon(old)} -> {_canon(new)}")
        lines += [f"{head}+ {_canon(r)}" for r in self.added]
        lines += [f"{head}- {_canon(r)}" for r in self.removed]
        return "\n".join(lines)


_MISSING = "<missing>"


def diff_records(
    old: Iterable[Mapping[str, Any]], new: Iterable[Mapping[str, Any]], key: list[str] | None = None
) -> Diff:
    """Compare two record lists ignoring order. With `key`, records with the same key values are
    compared field by field; without it, a record is either identical or added/removed."""
    a, b = _keyed(old, key), _keyed(new, key)
    d = Diff()
    for k in sorted(set(a) | set(b)):
        olds, news = a.get(k, []), b.get(k, [])
        for o, n in zip(olds, news, strict=False):
            fields = {
                f: (o.get(f, _MISSING), n.get(f, _MISSING))
                for f in sorted(set(o) | set(n))
                if o.get(f, _MISSING) != n.get(f, _MISSING)
            }
            if fields:
                d.changed.append((k, fields))
        d.removed += olds[len(news) :]
        d.added += news[len(olds) :]
    return d


def snapshot_path(directory: str | Path, step: str) -> Path:
    return Path(directory) / f"{step}.json"


def load_snapshot(directory: str | Path, step: str) -> list[dict] | None:
    p = snapshot_path(directory, step)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    return list(data)


def save_snapshot(
    directory: str | Path, step: str, records: Iterable[Mapping[str, Any]], key: list[str] | None = None
) -> Path:
    """Write records sorted by key (stable diffs in git)."""
    keyed = _keyed(records, key)
    rows = [r for k in sorted(keyed) for r in keyed[k]]
    p = snapshot_path(directory, step)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p
