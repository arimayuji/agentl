from __future__ import annotations

import atexit
import datetime as dt
import glob as _glob
import hashlib
import json
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .. import netguard
from ..registry import require

_TMP = Path(tempfile.mkdtemp(prefix="agentl-"))
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)


def tmpdir(prefix: str = "") -> Path:
    """A fresh directory under the run's temp area, removed when the process exits."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir=_TMP))


def download(url: str, headers: dict | None = None, timeout: float = 60, retries: int = 2) -> Path:
    """Download `url` to a temp file, keeping its file name, and return the path."""
    name = Path(urllib.parse.urlparse(url).path).name or "download"
    target = tmpdir("dl-") / name
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": "agentl", **(headers or {})})
        try:
            with netguard.urlopen(req, timeout=timeout) as resp, target.open("wb") as f:
                shutil.copyfileobj(resp, f)
            return target
        except urllib.error.HTTPError as e:
            if (e.code < 500 and e.code != 429) or attempt == retries:
                raise
        except urllib.error.URLError:
            if attempt == retries:
                raise
        time.sleep(2**attempt)
    raise RuntimeError("unreachable")


_REMOTE = re.compile(r"^(?!https?://|file://)[a-zA-Z][a-zA-Z0-9+.-]*://")
_ORIGIN: dict[Path, str] = {}


def is_remote(path: str) -> bool:
    """s3://, gs://, az://, abfs://, sftp://, ftp://, smb://, webdav... (anything fsspec knows)."""
    return bool(_REMOTE.match(path))


def _fs(uri: str, storage_options: dict | None) -> tuple[Any, str]:
    fsspec = require("fsspec", "remote")
    return fsspec.core.url_to_fs(uri, **(storage_options or {}))


def fetch_remote(uri: str, storage_options: dict | None = None) -> list[Path]:
    """Download the files matching a remote path or glob to the run's temp area."""
    fs, root = _fs(uri, storage_options)
    found = sorted(fs.glob(root)) if any(c in root for c in "*?[") else [root]
    found = [f for f in found if not fs.isdir(f)]
    if not found and not any(c in root for c in "*?["):
        raise FileNotFoundError(uri)
    out = []
    for remote in found:
        local = tmpdir("remote-") / Path(remote).name
        fs.get(remote, str(local))
        _ORIGIN[local] = fs.unstrip_protocol(remote)
        out.append(local)
    return out


def upload(local: Path, uri: str, storage_options: dict | None = None) -> None:
    fs, root = _fs(uri, storage_options)
    fs.put(str(local), root)


def origin(path: Path) -> str:
    """The remote URI a downloaded file came from, or the local path."""
    return _ORIGIN.get(path, str(path))


def paths(
    input: Any,
    path: str | None,
    field: str = "path",
    url: str | None = None,
    headers: dict | None = None,
    storage_options: dict | None = None,
) -> list[Path]:
    """Files from a `url`, a local or remote glob `path`, or the `field` of upstream records."""
    if url:
        return [download(url, headers)]
    if path and is_remote(path):
        return fetch_remote(path, storage_options)
    if path:
        found = sorted(p for p in _glob.glob(path, recursive=True) if Path(p).is_file())
        if not found and not any(c in path for c in "*?["):
            raise FileNotFoundError(path)
        return [Path(p) for p in found]
    if isinstance(input, list):
        out = []
        for r in input:
            ref = r.get(field)
            if not ref:
                continue
            if isinstance(ref, str) and ref.startswith(("http://", "https://")):
                out.append(download(ref, headers))
            elif isinstance(ref, str) and is_remote(ref):
                out.extend(fetch_remote(ref, storage_options))
            else:
                out.append(Path(ref))
        return out
    raise ValueError("set 'path' or 'url', or feed records that have a 'path' field")


def single(input: Any) -> list[dict]:
    if input is None:
        return []
    if isinstance(input, dict):
        raise ValueError("this connector takes a single input step")
    return input


ON_CONFLICT = ("update", "do_nothing", "error")


def upsert_options(
    name: str, mode: str, keys: list[str], on_conflict: str | None, update_columns: str | list[str] | None
) -> tuple[str, list[str] | None]:
    """Check `on_conflict` / `update_columns` before any I/O; shared with sqlite.write."""
    if mode != "upsert" and (on_conflict is not None or update_columns is not None):
        raise ValueError(f"{name}: on_conflict and update_columns need mode: upsert (got mode: {mode})")
    conflict = on_conflict or "update"
    if conflict not in ON_CONFLICT:
        raise ValueError(f"{name}: on_conflict must be one of {', '.join(ON_CONFLICT)}, got {on_conflict!r}")
    if update_columns is None:
        return conflict, None
    if conflict != "update":
        raise ValueError(f"{name}: update_columns only applies to on_conflict: update")
    upd = [update_columns] if isinstance(update_columns, str) else list(update_columns)
    if not upd or not all(isinstance(c, str) and c for c in upd):
        raise ValueError(f"{name}: update_columns must be a column name or a non-empty list of names")
    if in_key := [c for c in upd if c in keys]:
        raise ValueError(f"{name}: update_columns cannot include key column(s) {in_key}")
    return conflict, upd


DELETES = ("none", "missing", "partition_replaced")


def changeset_options(
    name: str,
    mode: str,
    keys: list[str],
    deletes: str | dict | None,
    partition_by: str | list[str] | None,
    dry_run: bool,
    checksums: bool = False,
) -> tuple[str, list[str]]:
    """Check the mode: changeset options before any I/O. `deletes` takes a name or `{when: name}`."""
    if mode != "changeset":
        if deletes is not None or partition_by is not None or dry_run or checksums:
            raise ValueError(
                f"{name}: deletes, partition_by, checksums and dry_run need mode: changeset "
                f"(got mode: {mode})"
            )
        return "none", []
    if not keys:
        raise ValueError(f"{name}: mode: changeset needs `key` (the column, or columns, that identify a row)")
    when = deletes.get("when") if isinstance(deletes, dict) else deletes
    if isinstance(deletes, dict) and set(deletes) - {"when"}:
        raise ValueError(f"{name}: deletes takes only `when`, got {sorted(deletes)}")
    drop = when or "none"
    if drop not in DELETES:
        raise ValueError(f"{name}: deletes must be one of {', '.join(DELETES)}, got {when!r}")
    parts = [partition_by] if isinstance(partition_by, str) else list(partition_by or [])
    if not all(isinstance(p, str) and p for p in parts):
        raise ValueError(f"{name}: partition_by must be a column name or a list of names")
    if drop == "partition_replaced" and not parts:
        raise ValueError(f"{name}: deletes: partition_replaced needs partition_by")
    if checksums and not parts:
        raise ValueError(f"{name}: checksums needs partition_by (a checksum is kept per partition)")
    return drop, parts


def emit_option(name: str, mode: str, emit: str | None) -> bool:
    """True for `emit: changes` (#289): the writer returns the changed records, not the counts."""
    if emit in (None, "counts"):
        return False
    if emit != "changes":
        raise ValueError(f"{name}: emit must be counts or changes, got {emit!r}")
    if mode != "changeset":
        raise ValueError(f"{name}: emit: changes needs mode: changeset (got mode: {mode})")
    return True


def before(new: dict, old: Any, cols: list[str]) -> dict:
    """`_before` of an updated record: the stored value of each of `cols` that differs."""
    return {c: old.get(c) for c in cols if new.get(c) != old.get(c)}


CHECKSUMS_TABLE = "_agentl_checksums"


class PartitionSums:
    """Order-independent checksum of the source records of each partition, built chunk by chunk:
    the count and the sum (mod 2**256) of each record's SHA-256. A partition whose checksum equals
    the one stored by the last changeset write is skipped without comparing its rows."""

    def __init__(self, parts: list[str]) -> None:
        self.parts = parts
        self.sums: dict[str, list[int]] = {}
        self.values: dict[str, tuple] = {}

    def add(self, rows: list[dict]) -> None:
        for r in rows:
            vals = tuple(r.get(p) for p in self.parts)
            part = partition_id(vals)
            line = json.dumps(r, sort_keys=True, default=str, separators=(",", ":"))
            h = int.from_bytes(hashlib.sha256(line.encode()).digest(), "big")
            acc = self.sums.get(part)
            if acc is None:
                self.sums[part] = [1, h]
                self.values[part] = vals
            else:
                acc[0] += 1
                acc[1] = (acc[1] + h) % (1 << 256)

    def digests(self) -> dict[str, str]:
        return {p: f"{n}:{h:064x}" for p, (n, h) in self.sums.items()}

    def equal(self, stored: dict[str, str]) -> list[tuple]:
        """Partition values whose checksum matches `stored` (partition id -> digest)."""
        return [self.values[p] for p, d in self.digests().items() if stored.get(p) == d]


def partition_id(values: tuple) -> str:
    """Text id of a partition's values; dates and times as ISO 8601, so a value read back from a
    table matches the string or date the source had."""
    return json.dumps(list(values), default=_iso, separators=(",", ":"))


def _iso(v: Any) -> str:
    return v.isoformat() if isinstance(v, (dt.date, dt.datetime, dt.time)) else str(v)


def null_keys(name: str, rows: list[dict], keys: list[str]) -> tuple[list[dict], list[dict]]:
    """Split upsert records into (good, rejected): a record with a null (or missing) key field
    cannot be matched to a row, so it goes to the rejected records with the reason in `_error`."""
    if len(keys) == 1:
        k = keys[0]
        if all(r.get(k) is not None for r in rows):
            return rows, []
        bad_of = [r for r in rows if r.get(k) is None]
    else:
        bad_of = [r for r in rows if any(r.get(k) is None for k in keys)]
        if not bad_of:
            return rows, []
    ids = {id(r) for r in bad_of}
    good = [r for r in rows if id(r) not in ids]
    bad = [
        {**r, "_error": f"{name}: upsert key {[k for k in keys if r.get(k) is None]} is null"} for r in bad_of
    ]
    return good, bad


def check_update_columns(name: str, upd: list[str] | None, cols: list[str]) -> None:
    if upd and (unknown := [c for c in upd if c not in cols]):
        raise ValueError(f"{name}: update_columns names unknown column(s) {unknown}; columns: {cols}")


SCD2_COLUMNS = ("valid_from", "valid_to", "is_current")
ON_MISSING = ("keep", "close")


def scd2_options(
    name: str,
    mode: str,
    keys: list[str],
    track: str | list[str] | None,
    on_missing: str | None,
    as_of: Any,
) -> tuple[list[str] | None, str, Any]:
    """Check the `mode: scd2` settings before any I/O. Returns (track, on_missing, as_of in UTC)."""
    if mode != "scd2":
        if track is not None or on_missing is not None or as_of is not None:
            raise ValueError(f"{name}: track, on_missing and as_of need mode: scd2 (got mode: {mode})")
        return None, "keep", None
    if not keys:
        raise ValueError(f"{name}: mode: scd2 needs `key` (the column, or columns, that identify a row)")
    if reserved := [k for k in keys if k in SCD2_COLUMNS]:
        raise ValueError(f"{name}: key cannot be the scd2 column(s) {reserved}")
    missing = on_missing or "keep"
    if missing not in ON_MISSING:
        raise ValueError(f"{name}: on_missing must be one of {', '.join(ON_MISSING)}, got {on_missing!r}")
    cols: list[str] | None = None
    if track is not None:
        cols = [track] if isinstance(track, str) else list(track)
        if not cols or not all(isinstance(c, str) and c for c in cols):
            raise ValueError(f"{name}: track must be a column name or a non-empty list of names")
        if bad := [c for c in cols if c in keys or c in SCD2_COLUMNS]:
            raise ValueError(f"{name}: track cannot include key or scd2 column(s) {bad}")
    if as_of is None:
        when = dt.datetime.now(dt.timezone.utc)
    else:
        if isinstance(as_of, str):
            try:
                as_of = dt.datetime.fromisoformat(
                    as_of[:-1] + "+00:00" if as_of.endswith(("Z", "z")) else as_of
                )
            except ValueError:
                raise ValueError(f"{name}: as_of must be an ISO 8601 timestamp, got {as_of!r}") from None
        if not isinstance(as_of, dt.datetime):
            raise ValueError(f"{name}: as_of must be a timestamp with a time zone, got {as_of!r}")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError(f"{name}: as_of needs a time zone (for example 2026-01-31T00:00:00Z)")
        when = as_of.astimezone(dt.timezone.utc)
    return cols, missing, when


def check_scd2_columns(name: str, track: list[str] | None, cols: list[str]) -> None:
    if reserved := [c for c in cols if c in SCD2_COLUMNS]:
        raise ValueError(f"{name}: mode: scd2 manages {list(SCD2_COLUMNS)}; records cannot carry {reserved}")
    if track and (unknown := [c for c in track if c not in cols]):
        raise ValueError(f"{name}: track names unknown column(s) {unknown}; columns: {cols}")
