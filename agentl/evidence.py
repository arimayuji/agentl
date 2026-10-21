"""Evidence (#293): keep the raw artifacts behind the records a source step captured.

`evidence:` on `http.get`, `http.post`, `web.crawl` and `browser.scrape` stores what the source
returned, content-addressed by sha256 in the same layout as the raw zone
(`objects/<aa>/<sha256><ext>`, written once, never overwritten), so an artifact seen by many runs
is kept once:

    evidence: {raw: true, request: true, screenshot: true, when: changed, storage: s3://bucket/ev}

- `raw`: the response body (the page HTML for `browser.scrape`);
- `request`: a JSON of the request and response metadata (method, URL, status, headers with
  credentials redacted, timing, ETag and Last-Modified);
- `screenshot`: a full-page PNG (`browser.scrape` only: `web.crawl` does not render pages);
- `when`: `changed` (default) stores and indexes a capture only when its content hash differs
  from the last one indexed for the same pipeline, step and URL; `always` every time;
  `on_failure` only for answers that failed (HTTP 4xx/5xx, or a page that gave no records).
- `path` (local folder) or `storage` (any fsspec URL, with `storage_options`): where the
  objects go. Default: $AGENTL_EVIDENCE_DIR, else `evidence/` next to the SQLite run store, else
  `.agentl/evidence`. `max_bytes` caps one artifact (default 50 MB; larger ones are skipped).

Each output record gets `_evidence` (the sha256 of its main artifact: raw, else screenshot, else
request) and `_evidence_<kind>` for the others. Inside a run, the run store indexes every stored
artifact (run_id, pipeline, step, url, kind, sha256, bytes, captured_at, location): `agentl
evidence ls [run_id]`, `agentl evidence show <sha>` and `agentl evidence verify`."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import raw
from .registry import current_run_id, current_step, require, run_store
from .snapshots import redact_headers, redact_url

log = logging.getLogger("agentl.evidence")

KINDS = ("raw", "screenshot", "request")
WHEN = ("changed", "always", "on_failure")
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
SUFFIX = {"raw": ".body", "screenshot": ".png", "request": ".json"}
CONTENT_TYPE = {"screenshot": "image/png", "request": "application/json"}
KEYS = frozenset({*KINDS, "when", "path", "storage", "storage_options", "max_bytes"})


class Backend:
    """Content-addressed objects in a local folder or behind an fsspec URL."""

    def __init__(self, location: str, storage_options: dict | None = None):
        self.location = location
        self.remote = "://" in location and not location.startswith("file://")
        self.fs: Any = None
        if self.remote:
            fsspec = require("fsspec", "remote")
            self.fs, self.prefix = fsspec.core.url_to_fs(location, **(storage_options or {}))
        else:
            self.base = Path(location.removeprefix("file://"))

    def key(self, sha: str, suffix: str) -> str:
        return f"objects/{sha[:2]}/{sha}{suffix}"

    def put(self, sha: str, suffix: str, data: bytes) -> None:
        if not self.remote:
            raw.write_new(raw.object_path(self.base, sha, suffix), data)
            return
        path = f"{self.prefix.rstrip('/')}/{self.key(sha, suffix)}"
        if not self.fs.exists(path):
            self.fs.pipe_file(path, data)

    def get(self, sha: str, suffix: str) -> bytes:
        if not self.remote:
            return raw.object_path(self.base, sha, suffix).read_bytes()
        return bytes(self.fs.cat_file(f"{self.prefix.rstrip('/')}/{self.key(sha, suffix)}"))


def default_location(store: Any = None) -> str:
    env = os.environ.get("AGENTL_EVIDENCE_DIR")
    if env:
        return env
    path = getattr(store, "path", None)
    if isinstance(path, Path) and path.suffix:
        return str(path.parent / "evidence")
    return ".agentl/evidence"


@dataclass
class Options:
    raw: bool = True
    request: bool = False
    screenshot: bool = False
    when: str = "changed"
    location: str = ""
    storage_options: dict = field(default_factory=dict)
    max_bytes: int = DEFAULT_MAX_BYTES


def options(value: Any, connector: str, screenshot: bool = False) -> Options | None:
    """`evidence:` of a step as Options, None when off; ValueError on a bad setting."""
    if not value:
        return None
    if value is True:
        value = {"raw": True}
    if not isinstance(value, dict):
        raise ValueError(f"{connector}: evidence takes true or a mapping ({', '.join(sorted(KEYS))})")
    unknown = set(value) - KEYS
    if unknown:
        raise ValueError(f"{connector}: evidence does not take {sorted(unknown)[0]!r}")
    if value.get("screenshot") and not screenshot:
        raise ValueError(f"{connector}: evidence.screenshot needs a browser (browser.scrape)")
    when = str(value.get("when", "changed"))
    if when not in WHEN:
        raise ValueError(f"{connector}: evidence.when must be one of {', '.join(WHEN)}")
    if value.get("path") and value.get("storage"):
        raise ValueError(f"{connector}: evidence takes path or storage, not both")
    opts = Options(
        raw=bool(value.get("raw", not (value.get("request") or value.get("screenshot")))),
        request=bool(value.get("request", False)),
        screenshot=bool(value.get("screenshot", False)),
        when=when,
        location=str(value.get("storage") or os.path.expanduser(str(value.get("path") or ""))),
        storage_options=dict(value.get("storage_options") or {}),
        max_bytes=int(value.get("max_bytes", DEFAULT_MAX_BYTES)),
    )
    if not (opts.raw or opts.request or opts.screenshot):
        raise ValueError(f"{connector}: evidence keeps nothing; set raw, request or screenshot")
    return opts


def request_metadata(
    method: str,
    url: str,
    status: int | None,
    request_headers: Any = None,
    response_headers: Any = None,
    elapsed: float | None = None,
) -> dict:
    """What `request: true` keeps, credentials redacted."""
    resp = dict(response_headers.items()) if response_headers is not None else {}
    lower = {k.lower(): v for k, v in resp.items()}
    return {
        "method": method,
        "url": redact_url(url),
        "status": status,
        "request_headers": redact_headers(dict(request_headers or {})),
        "response_headers": redact_headers(resp),
        "etag": lower.get("etag"),
        "last_modified": lower.get("last-modified"),
        "elapsed_ms": None if elapsed is None else round(elapsed * 1000, 1),
    }


class Recorder:
    """Stores and indexes the captures of one step call."""

    def __init__(self, opts: Options):
        self.opts = opts
        self.store = run_store()
        self.run_id = current_run_id()
        self.step = current_step()
        self.backend = Backend(opts.location or default_location(self.store), opts.storage_options)

    def capture(
        self,
        url: str,
        body: bytes | None = None,
        request: dict | None = None,
        screenshot: bytes | None = None,
        failed: bool = False,
        content_type: str | None = None,
    ) -> dict[str, str]:
        """Keep one capture; returns the `_evidence*` fields for its records (empty: not kept)."""
        o = self.opts
        if o.when == "on_failure" and not failed:
            return {}
        parts: dict[str, bytes] = {}
        if o.raw and body is not None:
            parts["raw"] = body
        if o.screenshot and screenshot is not None:
            parts["screenshot"] = screenshot
        change_key = None
        if o.request and request is not None:
            stable = {k: v for k, v in request.items() if k != "elapsed_ms"}
            change_key = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()
            parts["request"] = json.dumps(request, sort_keys=True, indent=1).encode()
        if not parts:
            return {}
        shas = {k: hashlib.sha256(v).hexdigest() for k, v in parts.items()}
        primary = next(k for k in KINDS if k in parts)
        where = redact_url(url)
        step = self.step if self.store is not None and self.run_id is not None else None
        if o.when == "changed" and step is not None:
            last = self.store.evidence_last(step[0], step[1], where, primary, self.backend.location)
            if last is not None and self._same(primary, last, shas[primary], change_key):
                return {"_evidence": last}  # unchanged: the records point to the stored capture
        now = time.time()
        for kind, data in parts.items():
            if len(data) > o.max_bytes:
                log.warning("evidence: %s of %s is %d bytes, over max_bytes", kind, where, len(data))
                shas.pop(kind)
                continue
            self.backend.put(shas[kind], SUFFIX[kind], data)
            if step is not None:
                self.store.evidence_add(
                    {
                        "run_id": self.run_id,
                        "pipeline": step[0],
                        "step": step[1],
                        "url": where,
                        "kind": kind,
                        "sha256": shas[kind],
                        "bytes": len(data),
                        "content_type": CONTENT_TYPE.get(kind, content_type),
                        "captured_at": now,
                        "location": self.backend.location,
                    }
                )
        if not shas:
            return {}
        return self._fields(next(k for k in KINDS if k in shas), shas)

    def _same(self, kind: str, last: str, sha: str, change_key: str | None) -> bool:
        if kind != "request":
            return last == sha
        try:  # the request JSON carries timing: compare without it
            old = json.loads(self.backend.get(last, SUFFIX["request"]))
        except (OSError, ValueError):
            return False
        old.pop("elapsed_ms", None)
        return hashlib.sha256(json.dumps(old, sort_keys=True).encode()).hexdigest() == change_key

    @staticmethod
    def _fields(primary: str, shas: dict[str, str]) -> dict[str, str]:
        out = {"_evidence": shas[primary]}
        out.update({f"_evidence_{k}": s for k, s in shas.items() if k != primary})
        return out


def recorder(value: Any, connector: str, screenshot: bool = False) -> Recorder | None:
    opts = options(value, connector, screenshot)
    return Recorder(opts) if opts else None


def tag(records: list[dict], fields: dict[str, str]) -> list[dict]:
    if fields:
        for r in records:
            if isinstance(r, dict):
                r.update(fields)
    return records


def read(row: dict) -> bytes:
    """The bytes of an indexed artifact, checked against its sha256 (ValueError when changed)."""
    data = Backend(row["location"]).get(row["sha256"], SUFFIX.get(row["kind"], ".body"))
    if hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise ValueError(f"evidence {row['sha256'][:12]} changed in storage (hash differs)")
    return data


def verify(rows: list[dict]) -> list[tuple[dict, str]]:
    """(row, problem) for each indexed artifact that is missing or whose hash differs."""
    bad: list[tuple[dict, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        key = (row["location"], row["sha256"], row["kind"])
        if key in seen:
            continue
        seen.add(key)
        try:
            read(row)
        except (OSError, FileNotFoundError):
            bad.append((row, "missing"))
        except ValueError:
            bad.append((row, "hash differs"))
    return bad
