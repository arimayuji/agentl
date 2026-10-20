"""`agentl diff`: what a run would insert, update and delete in each database writer, without writing.

The pipeline runs as it is, except that every keyed database writer (`sqlite.write`,
`postgres.write`, `sql.write` with `key`) becomes a `mode: changeset` write with `dry_run: true`
and `emit: changes`: it compares the records with the table and reports the changes, and the
table is left as it is. Other writers (files, HTTP, queues, ...) are replaced by a pass-through
and only their record count is shown. Notifications are off, and the run uses a copy of the run
store, so the agent cache is read but the real store records nothing.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .spec import LLMSpec, PipelineSpec, SpecError, StepSpec
from .testing import LOCAL_READERS, PURE

DB_WRITERS = ("sqlite.write", "postgres.write", "sql.write")
# options of the other modes that a changeset write does not take
_NOT_CHANGESET = ("on_conflict", "update_columns", "track", "on_missing", "as_of", "emit")
CHANGES = ("inserted", "updated", "deleted")


@dataclass
class WriterDiff:
    step: str
    use: str
    table: str | None = None
    counts: dict[str, int] = field(default_factory=dict)  # inserted, updated, deleted, unchanged
    rows: list[dict] = field(default_factory=list)  # the changed records, with `_change`
    note: str | None = None  # why there is no diff for this writer

    @property
    def changed(self) -> bool:
        return any(self.counts.get(k) for k in CHANGES)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"step": self.step, "use": self.use, "table": self.table, **self.counts}
        if self.note:
            out["note"] = self.note
        else:
            out["changes"] = self.rows
        return out

    def text(self, limit: int = 10) -> str:
        if self.note:
            return f"  {self.step:<14} {self.use:<15} {self.note}"
        c = self.counts
        lines = [
            f"  {self.step:<14} {self.use:<15} {self.table}: +{c.get('inserted', 0)} inserted, "
            f"~{c.get('updated', 0)} updated, -{c.get('deleted', 0)} deleted, "
            f"{c.get('unchanged', 0)} unchanged"
        ]
        sign = {"inserted": "+", "updated": "~", "deleted": "-"}
        for r in self.rows[:limit]:
            body = {k: v for k, v in r.items() if k not in ("_change", "_before")}
            lines.append(f"      {sign.get(r.get('_change', ''), '?')} {_show(body)}")
            for name, old in (r.get("_before") or {}).items():
                lines.append(f"          {name}: {_show(old)} -> {_show(r.get(name))}")
        if len(self.rows) > limit:
            lines.append(f"      ... and {len(self.rows) - limit} more change(s)")
        return "\n".join(lines)


@dataclass
class DiffResult:
    pipeline: str
    status: str
    error: str | None
    writers: list[WriterDiff]

    @property
    def changed(self) -> bool:
        return any(w.changed for w in self.writers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline": self.pipeline,
            "status": self.status,
            "error": self.error,
            "changed": self.changed,
            "writers": [w.to_dict() for w in self.writers],
        }

    def text(self, limit: int = 10) -> str:
        head = f"diff for {self.pipeline} (nothing is written)"
        lines = [head, ""]
        if self.status != "succeeded":
            lines.append(f"FAIL run {self.status}: {self.error or ''}")
        lines += [w.text(limit) for w in self.writers] or ["  no writer steps"]
        return "\n".join(lines)


def _show(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def _dry(s: StepSpec) -> StepSpec | str:
    """The dry changeset version of a database writer, or why it has none."""
    w = dict(s.with_)
    mode = w.get("mode", "append")
    if not w.get("key"):
        return f"no diff without `key` (mode: {mode}); not written"
    if mode == "scd2":
        return "no diff for mode: scd2; not written"
    if mode != "changeset":
        for k in _NOT_CHANGESET:
            w.pop(k, None)
        w.pop("deletes", None)
        w.pop("partition_by", None)
        w.pop("checksums", None)
        # replace drops the table, so rows it no longer has are deleted; upsert and append keep them
        w["deletes"] = "missing" if mode == "replace" else "none"
    w.update(mode="changeset", dry_run=True, emit="changes")
    return s.model_copy(update={"with_": w})


def prepare(
    spec: PipelineSpec, fixtures: dict[str | None, str], echo: bool = False
) -> tuple[PipelineSpec, dict[str, WriterDiff]]:
    """The spec a diff runs, and one entry per writer step (diffed or not)."""
    sources = [s for s in spec.steps if not s.inputs and s.kind == "use" and s.use != "trigger"]
    ids = {s.id for s in spec.steps}
    fixtures = dict(fixtures)
    if None in fixtures:
        if len(sources) != 1:
            names = ", ".join(s.id for s in sources) or "none"
            raise SpecError(f"the pipeline has several source steps ({names}): use --fixture STEP=PATH")
        fixtures = {sources[0].id: fixtures.pop(None), **fixtures}
    for sid, path in fixtures.items():
        if sid not in ids:
            raise SpecError(f"--fixture: no step '{sid}'")
        if not Path(str(path)).exists():
            raise FileNotFoundError(f"fixture not found: {path}")
    steps: list[StepSpec] = []
    writers: dict[str, WriterDiff] = {}
    for s in spec.steps:
        if s.id in fixtures:
            if s.use in LOCAL_READERS:  # same reader and options, so the records look the same
                s = s.model_copy(update={"with_": {**s.with_, "path": str(fixtures[s.id])}})
            else:
                s = StepSpec.model_validate(
                    {"id": s.id, "use": "file.read", "with": {"path": str(fixtures[s.id])}}
                )
        elif s.kind == "use" and s.inputs and s.use not in PURE and s.use not in LOCAL_READERS:
            use = s.use or ""
            table = s.with_.get("table")
            dry = _dry(s) if s.use in DB_WRITERS else "not a database writer; not written"
            if isinstance(dry, str):
                writers[s.id] = WriterDiff(s.id, use, table, note=dry)
                s = StepSpec.model_validate({"id": s.id, "use": "concat", "input": s.input})
            else:
                writers[s.id] = WriterDiff(s.id, use, table)
                s = dry
        steps.append(s)
    update: dict[str, Any] = {"steps": steps, "notify": None}
    if echo:
        update["llm"] = {
            name: LLMSpec(provider="echo", model=l.model, max_tokens=l.max_tokens)
            for name, l in spec.llm.items()
        }
        update["budget"] = None
    return spec.model_copy(update=update), writers


def _copy_store(source: str | None, target: Path) -> str:
    """A copy of a SQLite run store at `target` (the agent cache and state are read from it, and
    the real store records nothing). A Postgres store, or none yet, gives an empty one."""
    import os

    from .store import DEFAULT_DB

    src = str(source or os.environ.get("AGENTL_DB") or DEFAULT_DB)
    if not src.startswith(("postgresql://", "postgres://")) and Path(src).is_file():
        with closing(sqlite3.connect(src)) as a, closing(sqlite3.connect(target)) as b:
            a.backup(b)
    return str(target)


def run_diff(
    spec: PipelineSpec,
    fixtures: dict[str | None, str] | None = None,
    store: str | None = None,
    echo: bool = False,
) -> DiffResult:
    """Run `spec` with every database writer dry, and collect what each would change."""
    from .runner import Runner
    from .store import open_store

    dry_spec, writers = prepare(spec, dict(fixtures or {}), echo=echo)
    with tempfile.TemporaryDirectory() as tmp:
        st = open_store(_copy_store(store, Path(tmp) / "diff.db"))
        try:
            result = Runner(dry_spec, store=st, use_cache=True).run()
        finally:
            st.close()
    for sid, w in writers.items():
        out = result.outputs.get(sid)
        out = [] if out is None else out  # an empty Changes is falsy but still has its summary
        if w.note:
            w.note = f"{len(out)} record(s), {w.note}"
            continue
        summary = getattr(out, "summary", None) or []
        for r in summary:
            for k in (*CHANGES, "unchanged"):
                if isinstance(r.get(k), int):
                    w.counts[k] = w.counts.get(k, 0) + r[k]
        w.rows = json.loads(json.dumps(list(out), default=str))
    return DiffResult(spec.name, result.status, result.error, list(writers.values()))
