"""Golden sets (#154): run an agent step on records with a known answer and score it.

A golden file is JSONL. Each line is one record: the fields the step reads plus the expected
values of (some of) its `output` fields. The step runs on the record without those fields, with
the cache off, in a throwaway run store, and every expected field is compared with the answer:
text exactly, numbers within `golden.tolerance`.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .spec import GoldenSpec, PipelineSpec, StepSpec
from .store import Store

NO_ANSWER = "<no valid answer>"


@dataclass
class EvalReport:
    step: str
    label: str  # "default", or the override, e.g. "model=claude-haiku-5-5"
    records: int
    accuracy: float  # share of records with every expected field right
    fields: dict[str, float]  # field -> share of records where it is right
    cost_usd: float
    input_tokens: int
    output_tokens: int
    diffs: list[dict[str, Any]] = field(default_factory=list)  # {line, field, expected, got}
    min_accuracy: float | None = None

    @property
    def passed(self) -> bool:
        return self.min_accuracy is None or self.accuracy >= self.min_accuracy

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "label": self.label,
            "records": self.records,
            "accuracy": self.accuracy,
            "fields": self.fields,
            "cost_usd": round(self.cost_usd, 6),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "min_accuracy": self.min_accuracy,
            "passed": self.passed,
            "diffs": self.diffs,
        }


def golden_steps(spec: PipelineSpec, step_id: str | None = None) -> list[StepSpec]:
    if step_id is not None:
        step = spec.step(step_id)
        if step.golden is None:
            raise ValueError(f"step '{step_id}' has no golden set (golden: path/to/file.jsonl)")
        return [step]
    steps = [s for s in spec.steps if s.golden is not None]
    if not steps:
        raise ValueError(f"pipeline '{spec.name}' has no step with a golden set")
    return steps


def read_golden(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{n}: invalid JSON: {e.msg}") from None
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{n}: each line must be a JSON object")
            rows.append({"_line": n, **row})
    if not rows:
        raise ValueError(f"{path}: golden file is empty")
    return rows


def matches(expected: Any, got: Any, tolerance: float = 0.0) -> bool:
    numbers = (int, float)
    if isinstance(expected, numbers) and isinstance(got, numbers):
        if isinstance(expected, bool) or isinstance(got, bool):
            return expected is got
        return abs(float(expected) - float(got)) <= tolerance
    return bool(expected == got)


def _key(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, default=str)


def _apply(spec: PipelineSpec, step: StepSpec, override: dict[str, str]) -> PipelineSpec:
    if not override:
        return spec
    names = [step.llm, *step.escalate]
    llms = dict(spec.llm)
    for name in names:
        llms[name] = llms[name].model_copy(update=override)
    return spec.model_copy(update={"llm": llms})


def evaluate(
    spec: PipelineSpec,
    step: StepSpec,
    override: dict[str, str] | None = None,
    base_dir: str | Path | None = None,
) -> EvalReport:
    """Run `step` on its golden records and score the answers. `override` replaces fields of the
    step's llm settings (e.g. {"model": "other"}) for a side-by-side check."""
    from .runner import Runner

    golden: GoldenSpec | None = step.golden
    if golden is None:
        raise ValueError(f"step '{step.id}' has no golden set")
    path = Path(golden.file)
    if not path.is_absolute():
        path = Path(base_dir or spec.base_dir or ".") / path
    rows = read_golden(path)
    outputs = list(step.output or {})
    inputs: list[dict[str, Any]] = []
    expected: list[dict[str, Any]] = []
    for row in rows:
        inputs.append({k: v for k, v in row.items() if k != "_line" and k not in outputs})
        exp = {k: v for k, v in row.items() if k in outputs}
        if not exp:
            raise ValueError(
                f"{path}:{row['_line']}: no expected value for any output field ({', '.join(outputs)})"
            )
        expected.append(exp)

    one = step.model_copy(
        update={"input": None, "cache": False, "schema_": None, "rejected_to": None, "golden": None}
    )
    sub = _apply(spec, step, dict(override or {})).model_copy(
        update={"steps": [one], "chunk_size": None, "arrow": False, "trigger": [], "notify": None}
    )
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    with tmp, Store(str(Path(tmp.name) / "eval.db")) as store:
        result = Runner(sub, store=store, use_cache=False, replay={step.id: inputs}).run()
    if result.status != "succeeded":
        raise RuntimeError(f"step '{step.id}' failed on its golden set: {result.error}")

    answers: dict[str, dict[str, Any]] = {}
    for out in result.outputs.get(step.id, []):
        answers.setdefault(_key({k: v for k, v in out.items() if k not in outputs}), out)

    right = {f: 0 for f in outputs if any(f in e for e in expected)}
    seen = dict.fromkeys(right, 0)
    all_right = 0
    diffs: list[dict[str, Any]] = []
    for row, rec, exp in zip(rows, inputs, expected, strict=True):
        got = answers.get(_key(rec))
        ok = True
        for f, want in exp.items():
            seen[f] += 1
            value = got.get(f) if got is not None else NO_ANSWER
            if got is not None and matches(want, value, golden.tolerance):
                right[f] += 1
            else:
                ok = False
                diffs.append({"line": row["_line"], "field": f, "expected": want, "got": value})
        all_right += ok
    spend = result.spend.get(step.id, {})
    return EvalReport(
        step=step.id,
        label=", ".join(f"{k}={v}" for k, v in (override or {}).items()) or "default",
        records=len(rows),
        accuracy=all_right / len(rows),
        fields={f: right[f] / seen[f] for f in right},
        cost_usd=float(spend.get("cost_usd", result.cost_usd)),
        input_tokens=int(spend.get("input_tokens", result.input_tokens)),
        output_tokens=int(spend.get("output_tokens", result.output_tokens)),
        diffs=diffs,
        min_accuracy=golden.min_accuracy,
    )


def parse_override(text: str) -> dict[str, str]:
    """`model=other,provider=openai` -> {"model": "other", "provider": "openai"}."""
    out: dict[str, str] = {}
    for part in text.split(","):
        k, sep, v = part.partition("=")
        if not sep or not k.strip() or k.strip() not in ("model", "provider"):
            raise ValueError(f"--compare takes model=NAME or provider=NAME, got '{part}'")
        out[k.strip()] = v.strip()
    return out


def render(reports: list[EvalReport], max_diffs: int = 20) -> str:
    lines: list[str] = []
    for r in reports:
        verdict = (
            "" if r.min_accuracy is None else (" ok" if r.passed else f" FAIL (min {r.min_accuracy:.0%})")
        )
        lines.append(
            f"{r.step} [{r.label}]: {r.accuracy:.1%} of {r.records} records right{verdict}, "
            f"${r.cost_usd:.4f} ({r.input_tokens} in / {r.output_tokens} out tokens)"
        )
        for f, acc in r.fields.items():
            lines.append(f"  {f}: {acc:.1%}")
        for d in r.diffs[:max_diffs]:
            lines.append(
                f"  line {d['line']} {d['field']}: expected {json.dumps(d['expected'], default=str)}, "
                f"got {json.dumps(d['got'], default=str)}"
            )
        if len(r.diffs) > max_diffs:
            lines.append(f"  ... {len(r.diffs) - max_diffs} more (use --json)")
    return "\n".join(lines)
