"""Raw zone (#208): keep the original payloads of `persist: raw` steps and reprocess them.

A step with `persist: raw` stores its output, as JSON, in an immutable file named by its sha256
(`objects/<aa>/<sha256>.json`), never overwritten, and a small manifest per run and step
(`runs/<run_id>/<step>.json`) that catalogues it: pipeline, step, hash, size, record count and
time. The folder is `raw.path` in the pipeline, else $AGENTL_RAW_DIR, else `raw/` next to the
SQLite run store, else `.agentl/raw`. `raw.keep_days` drops manifests older than that after each
run, and then every object no manifest references.

`agentl replay <pipeline> --from-raw <run_id>` runs the current pipeline code with each
`persist: raw` step's output read back from that run's originals: the raw steps and the steps
upstream of them (the source fetch) do not run, everything downstream does. Agent steps hit the
per-record cache, so a replay costs nothing new unless the prompt or the input changed."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import resume
from .spec import PipelineSpec
from .store import Store

DEFAULT_MAX_BYTES = 256 * 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReplayRefused(ValueError):
    """`agentl replay --from-raw` cannot replay this run; the message says why."""


def max_bytes() -> int:
    try:
        return int(os.environ.get("AGENTL_RAW_MAX_BYTES", DEFAULT_MAX_BYTES))
    except ValueError:
        return DEFAULT_MAX_BYTES


def root(spec: PipelineSpec | None, store: Store | None) -> Path:
    """The raw zone folder; see the module docstring for the order."""
    if spec is not None and spec.raw is not None and spec.raw.path:
        p = Path(os.path.expanduser(spec.raw.path))
        if not p.is_absolute() and spec.base_dir:
            p = Path(spec.base_dir) / p
        return p
    env = os.environ.get("AGENTL_RAW_DIR")
    if env:
        return Path(env)
    path = getattr(store, "path", None)
    if isinstance(path, Path):
        return path.parent / "raw"
    return Path(".agentl/raw")


def _safe(step_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", step_id) + "-" + hashlib.sha256(step_id.encode()).hexdigest()[:8]


def object_path(base: Path, sha: str, suffix: str = ".json") -> Path:
    return base / "objects" / sha[:2] / f"{sha}{suffix}"


def write_new(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically, only if it does not exist yet (never overwrite)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    linked = False
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        # fails if another writer got there first: same content anyway
        with contextlib.suppress(FileExistsError):
            os.link(tmp, path)
            linked = True
    finally:
        os.unlink(tmp)
    # read-only only after the temp name is gone: on Windows the attribute belongs to the file, so
    # both hard links would be read-only and the temp one could not be deleted
    if linked:
        os.chmod(path, 0o400)


def store_output(
    spec: PipelineSpec, store: Store, run_id: str, step_id: str, out: Any
) -> tuple[dict | None, str | None]:
    """Keep `out` as the original of `step_id` in run `run_id`: (manifest, None) or (None, why)."""
    text, reason = resume.encode(out, max_bytes(), "AGENTL_RAW_MAX_BYTES")
    if text is None:
        return None, reason
    data = text.encode()
    sha = hashlib.sha256(data).hexdigest()
    base = root(spec, store)
    write_new(object_path(base, sha), data)
    manifest = {
        "run_id": run_id,
        "pipeline": spec.name,
        "step": step_id,
        "sha256": sha,
        "bytes": len(data),
        "records": len(out),
        "stored_at": time.time(),
    }
    folder = base / "runs" / run_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{_safe(step_id)}.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return manifest, None


def manifests(base: Path) -> list[dict]:
    """Every catalogued original under `base`, oldest first."""
    out: list[dict] = []
    for f in (base / "runs").glob("*/*.json"):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(m, dict) and SHA256.match(str(m.get("sha256", ""))):
            m["_file"] = str(f)
            out.append(m)
    return sorted(out, key=lambda m: (m.get("stored_at") or 0, m.get("run_id", ""), m.get("step", "")))


def load(base: Path, sha: str) -> Any:
    """The original with hash `sha`, checked against it; ReplayRefused when missing or changed."""
    if not SHA256.match(sha):
        raise ReplayRefused(f"{sha!r} is not a sha256")
    p = object_path(base, sha)
    try:
        data = p.read_bytes()
    except OSError:
        raise ReplayRefused(f"original {sha[:12]} is missing from {base}") from None
    if hashlib.sha256(data).hexdigest() != sha:
        raise ReplayRefused(f"original {sha[:12]} changed on disk (hash differs)")
    return resume.decode(data.decode())


def prune(base: Path, keep_days: float, now: float | None = None) -> tuple[int, int]:
    """Drop manifests older than `keep_days`, then unreferenced objects: (manifests, objects)."""
    cutoff = (now or time.time()) - keep_days * 86400
    dropped = 0
    for m in manifests(base):
        if (m.get("stored_at") or 0) < cutoff:
            f = Path(m["_file"])
            f.unlink(missing_ok=True)
            dropped += 1
            with contextlib.suppress(OSError):
                f.parent.rmdir()
    live = {m["sha256"] for m in manifests(base)}
    objects = 0
    for p in (base / "objects").glob("*/*.json"):
        if p.stem not in live:
            with contextlib.suppress(FileNotFoundError):
                os.chmod(p, 0o600)  # objects are read-only; Windows will not delete them as such
            p.unlink(missing_ok=True)
            objects += 1
    return dropped, objects


def _upstream(spec: PipelineSpec, ids: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    todo = [d for i in ids for d in spec.step(i).depends]
    while todo:
        i = todo.pop()
        if i not in seen:
            seen.add(i)
            todo.extend(spec.step(i).depends)
    return seen


def plan(spec: PipelineSpec, store: Store, run_id: str) -> resume.Plan:
    """What a replay of run `run_id`'s originals restores and skips, or ReplayRefused."""
    if not resume.RUN_ID.match(run_id):
        raise ReplayRefused(f"{run_id!r} is not a run id")
    raw_steps = [s.id for s in spec.steps if s.persist == "raw"]
    if not raw_steps:
        raise ReplayRefused(f"pipeline {spec.name!r} has no step with `persist: raw`")
    base = root(spec, store)
    found = {m["step"]: m for m in manifests(base) if m.get("run_id") == run_id}
    if not found:
        raise ReplayRefused(f"no originals of run {run_id} under {base} (see `agentl raw ls`)")
    out = resume.Plan(resumed_from=run_id)
    for step_id in raw_steps:
        m = found.get(step_id)
        if m is None:
            raise ReplayRefused(f"run {run_id} has no original for step '{step_id}'")
        if m.get("pipeline") != spec.name:
            raise ReplayRefused(f"run {run_id} is of pipeline {m.get('pipeline')!r}, not {spec.name!r}")
        out.restored[step_id] = load(base, m["sha256"])
        out.checkpoints[step_id] = {"sha256": m["sha256"], "records": m.get("records")}
    above = _upstream(spec, raw_steps) - set(raw_steps)
    skipped = above | set(raw_steps)
    for step in spec.steps:
        if step.id in skipped:
            continue
        for dep in step.depends:
            if dep in above:
                raise ReplayRefused(
                    f"step '{step.id}' reads '{dep}', which runs before the raw zone; a replay does "
                    f"not fetch sources again, so read the `persist: raw` step instead"
                )
    out.done = above
    return out
