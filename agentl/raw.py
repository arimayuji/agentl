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
per-record cache, so a replay costs nothing new unless the prompt or the input changed.

Instead of one run, a `Selection` picks originals across runs by run id prefix, stored-at date
range and hash prefix (`--prefix`, `--since`/`--until`, `--sha`); each raw step then reads the
matching originals of every selected run, oldest first, one copy per hash. `since_step` replays
only that step and what depends on it: side branches are skipped."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
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


def parse_when(value: str, end: bool = False) -> float:
    """`2026-06-01` or an ISO date-time (UTC when no offset) as a timestamp. With `end`, a bare
    date means the end of that day, so `--until 2026-06-30` includes the 30th."""
    try:
        if len(value) == 10:
            d = date.fromisoformat(value)
            t = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()
            return t + 86400 if end else t
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise ReplayRefused(f"{value!r} is not a date (YYYY-MM-DD or ISO date-time)") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


@dataclass
class Selection:
    """Which originals a replay reads: every filter set must match (#208)."""

    run_id: str | None = None  # exactly this run
    prefix: str | None = None  # run id prefix; run ids start with the date, e.g. 202606
    since: float | None = None  # stored at or after (timestamp)
    until: float | None = None  # stored before (timestamp)
    sha: str | None = None  # hash prefix of the original

    def empty(self) -> bool:
        return not any(v is not None for v in vars(self).values())

    def matches(self, m: dict) -> bool:
        run_id, at = str(m.get("run_id", "")), m.get("stored_at") or 0
        return (
            (self.run_id is None or run_id == self.run_id)
            and (self.prefix is None or run_id.startswith(self.prefix))
            and (self.since is None or at >= self.since)
            and (self.until is None or at < self.until)
            and (self.sha is None or str(m["sha256"]).startswith(self.sha))
        )

    def describe(self) -> str:
        if self.run_id is not None and len([v for v in vars(self).values() if v is not None]) == 1:
            return self.run_id
        return " ".join(f"{k}={v}" for k, v in vars(self).items() if v is not None)


def _downstream(spec: PipelineSpec, step_id: str) -> set[str]:
    seen = {step_id}
    changed = True
    while changed:
        changed = False
        for s in spec.steps:
            if s.id not in seen and seen & set(s.depends):
                seen.add(s.id)
                changed = True
    return seen


def plan(
    spec: PipelineSpec, store: Store, select: str | Selection, since_step: str | None = None
) -> resume.Plan:
    """What a replay of the selected originals restores and skips, or ReplayRefused.

    `select` is a run id or a Selection across runs. With `since_step`, only that step and the
    steps depending on it run (plus the steps they read from, again); the rest is skipped."""
    sel = Selection(run_id=select) if isinstance(select, str) else select
    if sel.empty():
        raise ReplayRefused("pick the originals: a run id, --prefix, --since/--until or --sha")
    if sel.run_id is not None and not resume.RUN_ID.match(sel.run_id):
        raise ReplayRefused(f"{sel.run_id!r} is not a run id")
    if sel.sha is not None and not re.fullmatch(r"[0-9a-f]{1,64}", sel.sha):
        raise ReplayRefused(f"{sel.sha!r} is not a sha256 prefix")
    if since_step is not None and since_step not in {s.id for s in spec.steps}:
        raise ReplayRefused(f"--since-step: no step '{since_step}' in pipeline {spec.name!r}")
    raw_steps = [s.id for s in spec.steps if s.persist == "raw"]
    if not raw_steps:
        raise ReplayRefused(f"pipeline {spec.name!r} has no step with `persist: raw`")
    base = root(spec, store)
    what = f"run {sel.run_id}" if sel.describe() == sel.run_id else f"selection {sel.describe()}"
    chosen = [m for m in manifests(base) if sel.matches(m)]
    if not chosen:
        raise ReplayRefused(f"no originals of {what} under {base} (see `agentl raw ls`)")
    out = resume.Plan(resumed_from=sel.describe())
    for step_id in raw_steps:
        ms = [m for m in chosen if m["step"] == step_id]
        if not ms:
            raise ReplayRefused(f"{what} has no original for step '{step_id}'")
        records: list = []
        hashes: list[str] = []
        for m in ms:
            if m.get("pipeline") != spec.name:
                raise ReplayRefused(
                    f"run {m.get('run_id')} is of pipeline {m.get('pipeline')!r}, not {spec.name!r}"
                )
            if m["sha256"] in hashes:  # the same payload kept by several runs: read it once
                continue
            hashes.append(m["sha256"])
            records.extend(load(base, m["sha256"]))
        out.restored[step_id] = records
        out.checkpoints[step_id] = (
            {"sha256": hashes[0], "records": len(records)}
            if len(hashes) == 1
            else {"sha256": hashes, "runs": sorted({m["run_id"] for m in ms}), "records": len(records)}
        )
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
    if since_step is not None:
        if since_step in above:
            raise ReplayRefused(
                f"--since-step '{since_step}' runs before the raw zone; a replay starts at or after "
                f"a `persist: raw` step"
            )
        keep = _downstream(spec, since_step)
        feeds = _upstream(spec, keep) - keep - skipped  # what they read, between the raw zone and them
        for step in spec.steps:
            if step.id in feeds and resume._writes(step):
                raise ReplayRefused(
                    f"--since-step '{since_step}' reads '{step.id}', which writes to a sink; "
                    f"start the replay at or before '{step.id}'"
                )
        side = {s.id for s in spec.steps} - keep - feeds - skipped
        out.done |= side
    return out
