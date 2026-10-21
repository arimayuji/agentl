"""Golden sets (#154): run an agent step on records with a known answer and score it.

A golden file is JSONL. Each line is one record: the fields the step reads plus the expected
values of (some of) its `output` fields. The step runs on the record without those fields, with
the cache off, in a throwaway run store, and every expected field is compared with the answer:
text exactly, numbers within `golden.tolerance`.
"""

from __future__ import annotations

import json
import os
import subprocess
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
    right: int = 0  # records with every expected field right

    @property
    def cost_per_right(self) -> float | None:
        """What one right answer cost: the spend over the records it got fully right (#156).
        None when none is right."""
        return round(self.cost_usd / self.right, 6) if self.right else None

    @property
    def passed(self) -> bool:
        return self.min_accuracy is None or self.accuracy >= self.min_accuracy

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "label": self.label,
            "records": self.records,
            "accuracy": self.accuracy,
            "right": self.right,
            "cost_per_right": self.cost_per_right,
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
    prompt: str | None = None,
    label: str | None = None,
) -> EvalReport:
    """Run `step` on its golden records and score the answers. `override` replaces fields of the
    step's llm settings (e.g. {"model": "other"}) and `prompt` the step's agent instruction, for a
    side-by-side check."""
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
        update={
            "agent": prompt if prompt is not None else step.agent,
            "input": None,
            "cache": False,
            "schema_": None,
            "rejected_to": None,
            "golden": None,
        }
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
        # A hybrid step tags each answer with `_path` (rules or agent): not part of the input.
        answers.setdefault(_key({k: v for k, v in out.items() if k not in outputs and k != "_path"}), out)

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
        label=label or ", ".join(f"{k}={v}" for k, v in (override or {}).items()) or "default",
        records=len(rows),
        accuracy=all_right / len(rows),
        fields={f: right[f] / seen[f] for f in right},
        cost_usd=float(spend.get("cost_usd", result.cost_usd)),
        input_tokens=int(spend.get("input_tokens", result.input_tokens)),
        output_tokens=int(spend.get("output_tokens", result.output_tokens)),
        diffs=diffs,
        min_accuracy=golden.min_accuracy,
        right=all_right,
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


@dataclass
class Variant:
    """One `--compare` run: llm overrides, or the agent prompt at a git ref."""

    label: str
    override: dict[str, str] = field(default_factory=dict)
    ref: str | None = None


def parse_compare(text: str) -> Variant:
    """`prompt@HEAD~1` -> the prompt at that git ref; else `model=...`/`provider=...`."""
    if text.startswith("prompt@"):
        ref = text[len("prompt@") :].strip()
        if not ref or ref.startswith("-") or any(c.isspace() for c in ref):
            raise ValueError(f"--compare prompt@REF needs a git ref, got '{text}'")
        return Variant(label=f"prompt@{ref}", ref=ref)
    override = parse_override(text)
    return Variant(label=", ".join(f"{k}={v}" for k, v in override.items()), override=override)


def prompt_at(pipeline: str | Path, ref: str, step_id: str) -> str:
    """The `agent:` instruction of `step_id` in the pipeline file as it was at git `ref`."""
    from .spec import _Loader, parse

    path = Path(pipeline).resolve()
    try:
        done = subprocess.run(
            ["git", "-C", str(path.parent), "show", f"{ref}:./{path.name}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as e:
        raise ValueError(f"--compare prompt@{ref}: cannot run git: {e}") from None
    if done.returncode != 0:
        msg = done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "git show failed"
        raise ValueError(f"--compare prompt@{ref}: {msg}")
    import yaml

    try:
        raw = yaml.load(done.stdout, Loader=_Loader)
        old = parse(raw, os.environ)
    except Exception as e:
        raise ValueError(f"--compare prompt@{ref}: the pipeline at {ref} does not load: {e}") from None
    match = next((s for s in old.steps if s.id == step_id), None)
    if match is None or not match.agent:
        raise ValueError(f"--compare prompt@{ref}: no agent step '{step_id}' in the pipeline at {ref}")
    return match.agent


def compare(
    spec: PipelineSpec, step: StepSpec, variant: Variant, pipeline: str | Path | None = None
) -> EvalReport:
    if variant.ref is None:
        return evaluate(spec, step, variant.override, label=variant.label)
    if pipeline is None:
        raise ValueError("--compare prompt@REF needs the pipeline file")
    return evaluate(spec, step, prompt=prompt_at(pipeline, variant.ref, step.id), label=variant.label)


_CLIPPED = "\u2026"


def run_records(store: Store, run_id: str, step: StepSpec) -> tuple[list[dict[str, Any]], str]:
    """The answers `step` gave in run `run_id`, as golden lines (input plus answer), and where they
    came from: the step's checkpoint when it is still kept (failed runs), else the stored sample
    of its first records. Records with a value cut short in the sample are left out."""
    from . import resume

    run = store.run(run_id)
    if run is None:
        raise ValueError(f"no run {run_id} in the run store")
    rows: list[dict[str, Any]] | None = None
    source = "checkpoint"
    for e in store.events(run_id):
        if e["type"] == "checkpoint" and e["step"] == step.id:
            out = resume._load(store, e["data"] or {})
            if isinstance(out, list):
                rows = out
    if rows is None:
        sample = store.sample(run_id, step.id)
        if sample is None:
            raise ValueError(f"run {run_id} kept no records of step '{step.id}'")
        rows, source = sample["records"], f"sample ({len(sample['records'])} of {sample['total']} records)"
    outputs = set(step.output or {})
    lines = []
    for r in rows:
        if not isinstance(r, dict) or not outputs & set(r):
            continue
        if any(isinstance(v, str) and v.endswith(_CLIPPED) for v in r.values()):
            continue
        lines.append({k: v for k, v in r.items() if not k.startswith("_") and k != "confidence"})
    return lines, source


def append_lines(path: Path, lines: list[dict[str, Any]], outputs: list[str]) -> int:
    """Append golden lines to `path`, skipping inputs (the line without its `outputs` fields) it
    already has. Returns how many were added."""

    def key(rec: Any) -> str:
        return _key({k: v for k, v in rec.items() if k not in outputs}) if isinstance(rec, dict) else ""

    seen: set[str] = set()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    for line in text.splitlines():
        try:
            seen.add(key(json.loads(line)))
        except json.JSONDecodeError:
            continue
    new = []
    for rec in lines:
        if key(rec) not in seen:
            seen.add(key(rec))
            new.append(json.dumps(rec, ensure_ascii=False, default=str))
    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if not text or text.endswith("\n") else "\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(prefix + "\n".join(new) + "\n")
    return len(new)


def golden_path(spec: PipelineSpec, step: StepSpec) -> Path:
    if step.golden is None:
        raise ValueError(f"step '{step.id}' has no golden set (golden: path/to/file.jsonl)")
    path = Path(step.golden.file)
    return path if path.is_absolute() else Path(spec.base_dir or ".") / path


def render(reports: list[EvalReport], max_diffs: int = 20) -> str:
    lines: list[str] = []
    base: dict[str, EvalReport] = {}
    for r in reports:
        first = base.setdefault(r.step, r)
        verdict = (
            "" if r.min_accuracy is None else (" ok" if r.passed else f" FAIL (min {r.min_accuracy:.0%})")
        )
        lines.append(
            f"{r.step} [{r.label}]: {r.accuracy:.1%} of {r.records} records right{verdict}, "
            f"${r.cost_usd:.4f} ({r.input_tokens} in / {r.output_tokens} out tokens), "
            + ("no right answer" if r.cost_per_right is None else f"${r.cost_per_right:.6f} per right answer")
        )
        for f, acc in r.fields.items():
            delta = ""
            if first is not r and f in first.fields:
                delta = f"  (vs {first.label}: {acc - first.fields[f]:+.1%})"
            lines.append(f"  {f}: {acc:.1%}{delta}")
        for d in r.diffs[:max_diffs]:
            lines.append(
                f"  line {d['line']} {d['field']}: expected {json.dumps(d['expected'], default=str)}, "
                f"got {json.dumps(d['got'], default=str)}"
            )
        if len(r.diffs) > max_diffs:
            lines.append(f"  ... {len(r.diffs) - max_diffs} more (use --json)")
    return "\n".join(lines)
