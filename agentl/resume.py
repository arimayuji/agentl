"""Step checkpoints and `agentl resume` (#339).

While a run executes, every completed step gets a `checkpoint` event in the run store: an
output fingerprint and, when it is cheap and safe, the path of a JSON file under
`.agentl/checkpoints/<run_id>/` that holds the output. A failed run can then be resumed: steps that
completed are skipped (sinks included) and their outputs are read back, and the run goes on from
the first failed or pending step. Checkpoint files of a run are deleted when it succeeds."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import registry
from .spec import PipelineSpec, StepSpec, is_sink
from .store import Store

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
RUN_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class ResumeRefused(ValueError):
    """`agentl resume` cannot safely resume this run; the message says why."""


def enabled() -> bool:
    return os.environ.get("AGENTL_CHECKPOINTS", "1").lower() not in ("0", "false", "no")


def max_bytes() -> int:
    try:
        return int(os.environ.get("AGENTL_CHECKPOINT_MAX_BYTES", DEFAULT_MAX_BYTES))
    except ValueError:
        return DEFAULT_MAX_BYTES


def root(store: Store) -> Path:
    """Where checkpoint files go: $AGENTL_CHECKPOINT_DIR, else `checkpoints/` next to a SQLite store,
    else `.agentl/checkpoints` (a Postgres store keeps only the events; resume on the same host)."""
    env = os.environ.get("AGENTL_CHECKPOINT_DIR")
    if env:
        return Path(env)
    path = getattr(store, "path", None)
    if isinstance(path, Path):
        return path.parent / "checkpoints"
    return Path(".agentl/checkpoints")


def spec_hash(spec: PipelineSpec) -> str:
    """Hash of the resolved spec (variables and params included): resume needs the same one."""
    dumped = spec.model_dump(mode="json", by_alias=True, exclude={"base_dir"})
    return hashlib.sha256(json.dumps(dumped, sort_keys=True, default=str).encode()).hexdigest()


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def encode(out: Any) -> tuple[str | None, str | None]:
    """(JSON text, None) for an output that reads back unchanged, else (None, reason)."""
    if isinstance(out, registry.Changes):
        payload: dict[str, Any] = {"changes": list(out), "summary": out.summary}
    elif isinstance(out, list):
        payload = {"records": out}
    else:
        return None, f"output type {type(out).__name__} is not stored"
    try:
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return None, "output has values JSON cannot hold"
    if len(text.encode()) > max_bytes():
        return None, f"output is over the {max_bytes()} byte limit (AGENTL_CHECKPOINT_MAX_BYTES)"
    if json.loads(text) != payload:  # tuples, non-string keys: would not read back the same
        return None, "output does not read back unchanged from JSON"
    from . import secrets

    if any(h and h in text for h in secrets.sensitive()):
        return None, "output holds a secret value"
    return text, None


def decode(text: str) -> Any:
    payload = json.loads(text)
    if "changes" in payload:
        return registry.Changes(payload["changes"], payload["summary"])
    return payload["records"]


def write(store: Store, run_id: str, step_id: str, text: str) -> str:
    folder = root(store) / run_id
    folder.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", step_id) + "-" + _fingerprint(step_id)[:8] + ".json"
    path = folder / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return str(path)


def record(store: Store, run_id: str, step_id: str, out: Any, reason: str | None) -> None:
    """Store a `checkpoint` event; `reason` says why the output itself is not kept."""
    text = None
    if reason is None:
        text, reason = encode(out)
    if text is None:
        try:
            fp: str | None = _fingerprint(json.dumps(out, sort_keys=True, default=str))
        except Exception:
            fp = None
        store.event(run_id, "checkpoint", step_id, {"fingerprint": fp, "path": None, "reason": reason})
        return
    try:
        path: str | None = write(store, run_id, step_id, text)
    except OSError as e:
        path, reason = None, f"could not write the checkpoint: {e}"
    store.event(
        run_id,
        "checkpoint",
        step_id,
        {"fingerprint": _fingerprint(text), "path": path, "reason": reason},
    )


def remove(store: Store, run_ids: list[str]) -> None:
    base = root(store)
    for run_id in run_ids:
        if RUN_ID.match(run_id):
            shutil.rmtree(base / run_id, ignore_errors=True)


def _load(store: Store, data: dict) -> Any | None:
    """The stored output, or None when missing, outside the checkpoint folder or changed."""
    path = data.get("path")
    if not path:
        return None
    p = Path(path).resolve()
    if not p.is_relative_to(root(store).resolve()):
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    if _fingerprint(text) != data.get("fingerprint"):
        return None
    return decode(text)


@dataclass
class Plan:
    resumed_from: str
    restored: dict[str, Any] = field(default_factory=dict)  # step id -> output read back
    done: set[str] = field(default_factory=set)  # completed, output not needed (set to [])
    rerun: dict[str, str] = field(default_factory=dict)  # completed step run again -> why
    checkpoints: dict[str, dict] = field(default_factory=dict)  # carried over to the new run
    ancestors: list[str] = field(default_factory=list)  # run ids whose checkpoints we read


def _writes(step: StepSpec) -> bool:
    return any(is_sink(u) for u in (step.use, *(f.use for f in step.fallback)))


def plan(spec: PipelineSpec, store: Store, run_id: str, chains: dict[str, list[StepSpec]]) -> Plan:
    """What a resume of `run_id` restores and what it runs, or ResumeRefused."""
    run = store.run(run_id)
    if run is None:
        raise ResumeRefused(f"no run {run_id} in the run store")
    if run["pipeline"] != spec.name:
        raise ResumeRefused(f"run {run_id} is of pipeline {run['pipeline']!r}, not {spec.name!r}")
    if run["status"] != "failed":
        raise ResumeRefused(f"run {run_id} is {run['status']}; only a failed run can be resumed")
    events = store.events(run_id)
    started: dict = next((e["data"] for e in events if e["type"] == "run.started"), {}) or {}
    if not started.get("spec_hash"):
        raise ResumeRefused(f"run {run_id} has no spec hash (recorded before checkpoints); run it again")
    if started["spec_hash"] != spec_hash(spec):
        raise ResumeRefused(
            f"the pipeline changed since run {run_id} (spec hash differs: the YAML, its variables "
            "or params); resume needs the same spec, so run it again instead"
        )
    ids = {s.id for s in spec.steps}
    cps = {e["step"]: e["data"] for e in events if e["type"] == "checkpoint" and e["step"] in ids}
    out = Plan(resumed_from=run_id, ancestors=[run_id, *(started.get("resumed_chain") or [])])
    loaded: dict[str, Any] = {}

    def loadable(step_id: str) -> bool:
        if step_id not in loaded:
            loaded[step_id] = _load(store, cps[step_id])
        return loaded[step_id] is not None

    to_run = ids - set(cps)
    changed = True
    while changed:
        changed = False
        for chain in chains.values():
            members = {s.id for s in chain}
            if members & to_run and not members <= to_run:
                for s in chain:
                    if s.id not in to_run:
                        out.rerun[s.id] = "part of a chunked chain that runs again"
                to_run |= members
                changed = True
        for step in spec.steps:
            if step.id not in to_run:
                continue
            for dep in step.depends:
                if dep in cps and dep not in to_run and not loadable(dep):
                    why = cps[dep].get("reason") or "the stored output is missing or changed"
                    out.rerun[dep] = f"its output was not stored ({why})"
                    to_run.add(dep)
                    changed = True
    for step_id in out.rerun:
        step = spec.step(step_id)
        if _writes(step):
            raise ResumeRefused(
                f"step '{step_id}' is a writer that completed in run {run_id}, but resuming would "
                f"run it again: {out.rerun[step_id]}. Run the pipeline again instead (with an upsert "
                "or changeset writer that is safe)"
            )
    for step_id, data in cps.items():
        if step_id in to_run:
            continue
        out.checkpoints[step_id] = data
        if loadable(step_id):
            out.restored[step_id] = loaded[step_id]
        else:
            out.done.add(step_id)
    return out
