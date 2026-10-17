"""`agentl test`: run a pipeline on fixture files and compare what it would write with a golden file.

Source steps (steps with no input) read the fixtures instead, every LLM uses the offline `echo`
provider, writers and notifications are replaced so nothing leaves the machine, and the run uses
a throwaway store. The records each writer would have written are compared with the expected ones.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .preflight import SAFE
from .spec import LLMSpec, PipelineSpec, SpecError, StepSpec

# Connectors that only transform records: they run as they are in a test. Any other connector
# step with inputs may write somewhere, so it is replaced by a pass-through.
PURE = SAFE | {
    "flatten",
    "explode",
    "pivot",
    "unpivot",
    "cast",
    "compute",
    "aggregate",
    "sort",
    "limit",
    "fill",
    "sql",
    "protect",
    "unprotect",
}
# Sources that read local files may run as they are; any other source needs a fixture.
LOCAL_READERS = {
    "file.read",
    "csv.read",
    "json.read",
    "txt.read",
    "excel.read",
    "parquet.read",
    "pdf.read",
    "docx.read",
    "doc.read",
    "xml.read",
    "html.read",
}
IGNORED = ("_file", "_archive")  # point at the fixture path, not at the data


@dataclass
class StepDiff:
    step: str
    expected: int
    actual: int
    missing: list[Any] = field(default_factory=list)  # expected rows not produced
    extra: list[Any] = field(default_factory=list)  # produced rows not expected
    changed: list[tuple[Any, dict[str, tuple[Any, Any]]]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.missing or self.extra or self.changed)

    def text(self, limit: int = 10) -> str:
        if self.ok:
            return f"ok  {self.step}: {self.actual} record(s) match"
        lines = [f"FAIL {self.step}: expected {self.expected} record(s), got {self.actual}"]
        for where, fields in self.changed[:limit]:
            lines.append(f"  ~ {where}")
            for name, (want, got) in fields.items():
                lines.append(f"      {name}: expected {_show(want)}, got {_show(got)}")
        for where in self.missing[:limit]:
            lines.append(f"  - missing {where}")
        for where in self.extra[:limit]:
            lines.append(f"  + unexpected {where}")
        hidden = sum(max(0, len(x) - limit) for x in (self.changed, self.missing, self.extra))
        if hidden:
            lines.append(f"  ... and {hidden} more difference(s)")
        return "\n".join(lines)


@dataclass
class TestResult:
    status: str  # the run's status
    error: str | None
    diffs: list[StepDiff]

    @property
    def ok(self) -> bool:
        return self.status == "succeeded" and all(d.ok for d in self.diffs)

    def text(self) -> str:
        head = [] if self.status == "succeeded" else [f"FAIL run {self.status}: {self.error or ''}"]
        return "\n".join(head + [d.text() for d in self.diffs])


def _show(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def _norm(rows: list[dict], ignore: tuple[str, ...]) -> list[dict]:
    rows = json.loads(json.dumps(list(rows or []), default=str))
    return [{k: v for k, v in r.items() if k not in ignore} for r in rows]


def compare(step: str, expected: list[dict], actual: list[dict], key: str | None = None) -> StepDiff:
    """Row diff: by `key` when given, else by position."""
    d = StepDiff(step, len(expected), len(actual))

    def fields(want: dict, got: dict) -> dict[str, tuple[Any, Any]]:
        names = list(want) + [k for k in got if k not in want]
        return {k: (want.get(k), got.get(k)) for k in names if want.get(k) != got.get(k)}

    if key is None:
        for i in range(max(len(expected), len(actual))):
            if i >= len(actual):
                d.missing.append(f"row {i}: {_show(expected[i])}")
            elif i >= len(expected):
                d.extra.append(f"row {i}: {_show(actual[i])}")
            elif expected[i] != actual[i]:
                d.changed.append((f"row {i}", fields(expected[i], actual[i])))
        return d
    got = {_show(r.get(key)): r for r in actual}
    want = {_show(r.get(key)): r for r in expected}
    for k, row in want.items():
        if k not in got:
            d.missing.append(f"{key}={k}: {_show(row)}")
        elif row != got[k]:
            d.changed.append((f"{key}={k}", fields(row, got[k])))
    d.extra += [f"{key}={k}: {_show(row)}" for k, row in got.items() if k not in want]
    return d


def prepare(spec: PipelineSpec, fixtures: dict[str | None, str]) -> tuple[PipelineSpec, list[str]]:
    """The spec a test runs, and the ids of the steps whose records are compared (the writers)."""
    sources = [s for s in spec.steps if not s.inputs and s.kind == "use" and s.use != "trigger"]
    ids = {s.id for s in spec.steps}
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
    writers: list[str] = []
    for s in spec.steps:
        if s.id in fixtures:
            s = StepSpec.model_validate(
                {"id": s.id, "use": "file.read", "with": {"path": str(fixtures[s.id])}}
            )
        elif s in sources and s.use not in LOCAL_READERS:
            raise SpecError(f"source step '{s.id}' ({s.use}) needs a fixture: --fixture {s.id}=PATH")
        elif s.kind == "use" and s.inputs and s.use not in PURE:
            writers.append(s.id)
            s = StepSpec.model_validate({"id": s.id, "use": "concat", "input": s.input})
        elif s.rejected_to is not None:
            s = s.model_copy(update={"rejected_to": None})
        steps.append(s)
    llms = {
        name: LLMSpec(provider="echo", model=l.model, max_tokens=l.max_tokens) for name, l in spec.llm.items()
    }
    test_spec = spec.model_copy(update={"steps": steps, "llm": llms, "notify": None, "budget": None})
    if not writers:  # nothing is written: compare the last step
        writers = [spec.steps[-1].id]
    return test_spec, writers


def run_test(
    spec: PipelineSpec,
    fixtures: dict[str | None, str],
    expect: str | Path,
    key: str | None = None,
    ignore: tuple[str, ...] = IGNORED,
    update: bool = False,
) -> TestResult:
    """Run `spec` on `fixtures` and compare with the JSON file `expect`: `{step: [records]}`, or a
    list of records when the pipeline has one writer. `update=True` writes the file instead."""
    from .runner import Runner
    from .store import open_store

    test_spec, compared = prepare(spec, dict(fixtures))
    with tempfile.TemporaryDirectory() as tmp:
        store = open_store(Path(tmp) / "test.db")
        try:
            result = Runner(test_spec, store=store, use_cache=False).run()
        finally:
            store.close()
    actual = {sid: _norm(result.outputs.get(sid) or [], ignore) for sid in compared}
    path = Path(expect)
    if update:
        body: Any = actual[compared[0]] if len(compared) == 1 else actual
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return TestResult(result.status, result.error, [])
    golden = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(golden, list):
        if len(compared) != 1:
            raise SpecError(
                f"{path}: a list fits one writer; use {{step: [records]}} for {', '.join(compared)}"
            )
        golden = {compared[0]: golden}
    unknown = [k for k in golden if k not in actual]
    if unknown:
        raise SpecError(f"{path}: {', '.join(unknown)} not compared (writers: {', '.join(compared)})")
    diffs = [compare(sid, _norm(rows, ignore), actual[sid], key) for sid, rows in golden.items()]
    return TestResult(result.status, result.error, diffs)
