from __future__ import annotations

import atexit
import glob as _glob
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
) -> tuple[str, list[str]]:
    """Check the mode: changeset options before any I/O. `deletes` takes a name or `{when: name}`."""
    if mode != "changeset":
        if deletes is not None or partition_by is not None or dry_run:
            raise ValueError(
                f"{name}: deletes, partition_by and dry_run need mode: changeset (got mode: {mode})"
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
    return drop, parts


def check_update_columns(name: str, upd: list[str] | None, cols: list[str]) -> None:
    if upd and (unknown := [c for c in upd if c not in cols]):
        raise ValueError(f"{name}: update_columns names unknown column(s) {unknown}; columns: {cols}")
