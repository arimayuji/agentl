"""`agentl repair`: a proposed fix for a failed run, as a unified diff against the pipeline YAML (#299).

The proposal is checked locally (diff applied in memory, then parsed and linted) and never applied
to the pipeline file. What reaches the model: the run's diagnosis (as `agentl doctor` sees it), the
pipeline YAML, lint findings and a few sample records that reached the failing step. Sample values
that look like personal data are replaced by their kind, and secret values (resolved secret
references, environment variables with secret-looking names) are masked everywhere in the payload.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from . import assist, lint, pii, secrets
from .spec import LLMSpec, PipelineSpec, SpecError

SAMPLE_ROWS = 3
VALUE_CHARS = 120


class Proposal(BaseModel):
    diagnosis: str
    confidence: float = Field(ge=0, le=1)
    patch: str  # unified diff against the pipeline YAML; empty when there is no YAML fix
    notes: list[str] = Field(default_factory=list)


INSTRUCTION = """You fix failed agentl pipeline runs. The record holds what the run store recorded
about the failure (`run`), the pipeline YAML (`pipeline_yaml`), lint findings (`lint`) and a few
sample records that reached the failing step (`samples`; personal data and secrets are masked).
Answer with:
- diagnosis: the likely cause, in a few sentences; say when the record is not enough to tell
- confidence: 0 to 1
- patch: a unified diff against the pipeline YAML (`--- pipeline.yaml`, `+++ pipeline.yaml`, `@@`
  hunks with exact context lines) that fixes it; empty string when the fix is not in the YAML
- notes: anything else the user must do (environment variables, permissions, data fixes)
Never put secret values in the patch: use ${VAR} references."""


class PatchError(ValueError):
    pass


# ---------------------------------------------------------------- unified diff
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def apply_patch(text: str, patch: str) -> str:
    """`text` with the unified diff `patch` applied. Hunks are located by their context and removed
    lines (their line numbers are only a hint), so a model's slightly wrong numbers still apply."""
    lines = text.splitlines()
    hunks: list[list[str]] = []
    starts: list[int] = []
    for raw in patch.splitlines():
        m = _HUNK.match(raw)
        if m:
            hunks.append([])
            starts.append(int(m.group(1)))
        elif hunks and (raw[:1] in (" ", "+", "-") or raw == ""):
            if raw.startswith(("--- ", "+++ ")) and not hunks[-1]:
                continue
            hunks[-1].append(raw)
        elif raw.startswith("\\"):  # "\ No newline at end of file"
            continue
    if not hunks:
        raise PatchError("the patch has no @@ hunks")
    out, pos = [], 0
    for hunk, start in zip(hunks, starts, strict=True):
        while hunk and hunk[-1] == "":
            hunk.pop()
        old = [h[1:] if h else "" for h in hunk if h[:1] in (" ", "-") or h == ""]
        new = [h[1:] if h else "" for h in hunk if h[:1] in (" ", "+") or h == ""]
        at = _find(lines, old, pos, max(start - 1, 0))
        if at is None:
            shown = "\n".join(old[:3])
            raise PatchError(f"hunk at line {start} does not match the pipeline:\n{shown}")
        out += lines[pos:at] + new
        pos = at + len(old)
    out += lines[pos:]
    return "\n".join(out) + ("\n" if text.endswith("\n") or not text else "")


def _find(lines: list[str], old: list[str], lo: int, hint: int) -> int | None:
    if not old:
        return max(lo, min(hint, len(lines)))
    hits = [i for i in range(lo, len(lines) - len(old) + 1) if lines[i : i + len(old)] == old]
    if not hits:  # tolerate trailing whitespace differences
        strip = [o.rstrip() for o in old]
        hits = [
            i
            for i in range(lo, len(lines) - len(old) + 1)
            if [x.rstrip() for x in lines[i : i + len(old)]] == strip
        ]
    return min(hits, key=lambda i: abs(i - hint)) if hits else None


# ---------------------------------------------------------------- redaction
def _hidden() -> set[str]:
    """Secret values to mask: what the process resolved plus environment variables whose names
    look like secrets."""
    env = {v for k, v in os.environ.items() if v and len(v) >= 6 and secrets.SECRET_NAME.search(k)}
    return secrets.sensitive() | env


def _redact_value(field: str, value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(field, v) for v in value[:5]]
    kind = pii.classify(value, field)
    if kind:
        return f"<{kind}>"
    if isinstance(value, str) and len(value) > VALUE_CHARS:
        return value[:VALUE_CHARS] + "..."
    return value


def redact_records(records: list[dict]) -> list[dict]:
    return [_redact_value("", r) for r in records[:SAMPLE_ROWS] if isinstance(r, dict)]


# ---------------------------------------------------------------- context
def _spec_of(path: str | None, text: str | None) -> PipelineSpec | None:
    """The pipeline for picking the llm: real environment first (provider options may need it),
    else placeholders, else None (a broken file)."""
    if path:
        from .spec import load

        try:
            return load(path)
        except Exception:
            pass
    if text:
        return lint.check_text(text, env=_Placeholders()).spec
    return None


def _Placeholders() -> Any:
    from .mcp_server import _Placeholders as P

    return P()


def _samples(store: Any, run_id: str, spec: PipelineSpec | None, step_id: str | None) -> dict:
    """Redacted samples of the records that reached `step_id`: its inputs' samples, else its own."""
    if not step_id:
        return {}
    step = next((s for s in spec.steps if s.id == step_id), None) if spec else None
    sources = list(step.inputs) if step is not None else []
    out = {}
    for sid in sources or [step_id]:
        got = store.sample(run_id, sid)
        if got and got.get("records"):
            out[sid] = redact_records(got["records"])
    return out


def context(
    store: Any, run_id: str, pipeline_text: str, pipeline_path: str | None, spec: PipelineSpec | None
) -> dict:
    """The record sent to the model, with secrets masked."""
    report = assist.diagnose(store, run_id)
    base = str(Path(pipeline_path).resolve().parent) if pipeline_path else None
    findings = lint.check_text(pipeline_text, pipeline_path or "pipeline.yaml", base, env=_Placeholders())
    record = {
        "run": {**report, "explanation": assist.explain(report)},
        "pipeline_yaml": pipeline_text,
        "lint": [f"{f.level} {f.code} line {f.line}: {f.message}" for f in findings.findings],
        "samples": _samples(store, run_id, spec, report["failed_step"]),
    }
    hidden = _hidden()
    if not hidden:
        return record
    masked: dict = json.loads(secrets.mask(json.dumps(record, default=str), hidden))
    return masked


# ---------------------------------------------------------------- llm
def pick_llm(spec: PipelineSpec | None, name: str | None) -> tuple[Any, LLMSpec]:
    from .llm import provider

    if spec is not None and spec.llm:
        key = name or ("default" if "default" in spec.llm else next(iter(spec.llm)))
        if key not in spec.llm:
            raise SpecError(f"no llm '{key}' in the pipeline (llms: {', '.join(spec.llm)})")
        cfg = spec.llm[key]
        return provider(cfg), cfg
    if name:
        raise SpecError(f"--llm {name}: the pipeline could not be loaded to find its llm section")
    choice = assist.llm_choice()
    cfg = LLMSpec(**choice, max_tokens=8192)
    return provider(cfg), cfg


def validate_patched(text: str, path: str | None) -> list[str]:
    """Errors of the patched YAML: spec loader plus lint. Empty when it is valid."""
    base = str(Path(path).resolve().parent) if path else None
    report = lint.check_text(text, path or "pipeline.yaml", base, warnings=False, env=_Placeholders())
    errs = [f"{f.code} line {f.line}: {f.message}" for f in report.errors]
    if not errs and report.spec is None:
        errs = ["the patched YAML did not load"]
    return errs


def propose(
    store: Any,
    run_id: str,
    pipeline_text: str,
    pipeline_path: str | None = None,
    llm_name: str | None = None,
    record_event: bool = True,
) -> dict:
    """A checked repair proposal for `run_id`. Nothing is written but an optional
    `repair.proposed` event."""
    run = store.run(run_id)
    if run is None:
        raise KeyError(f"no run '{run_id}' in this store")
    spec = _spec_of(pipeline_path, pipeline_text)
    llm, cfg = pick_llm(spec, llm_name)
    record = context(store, run_id, pipeline_text, pipeline_path, spec)
    budget = spec.budget if spec is not None else None
    usage = {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    result: dict[str, Any] = {
        "run_id": run_id,
        "pipeline": run["pipeline"],
        "status": run["status"],
        "failed_step": record["run"]["failed_step"],
        "diagnosis": None,
        "confidence": None,
        "notes": [],
        "patch": "",
        "valid": False,
        "errors": [],
        "attempts": 0,
        "usage": usage,
        "applied": False,
    }
    prompt = dict(record)
    for attempt in range(2):
        if attempt and budget is not None and _over(budget, usage):
            result["errors"].append("budget reached: no retry")
            break
        result["attempts"] = attempt + 1
        try:
            answer = llm.structured(INSTRUCTION, prompt, Proposal)
            value = Proposal.model_validate(answer.value.model_dump())
            _charge(usage, cfg, answer.input_tokens, answer.output_tokens, answer.cost_usd)
        except (ValidationError, ValueError) as e:
            _charge(usage, cfg, getattr(e, "input_tokens", 0), getattr(e, "output_tokens", 0), None)
            result["errors"] = [f"the model gave no valid proposal: {str(e)[:300]}"]
            continue
        result.update(
            diagnosis=value.diagnosis, confidence=value.confidence, notes=value.notes, patch=value.patch
        )
        errors = _check(pipeline_text, value.patch, pipeline_path)
        result["errors"] = errors
        if not errors:
            result["valid"] = True
            result["patched"] = apply_patch(pipeline_text, value.patch)
            break
        prompt = {**record, "previous_patch": value.patch, "validation_errors": errors}
    if not result["valid"]:
        result["patch"] = ""  # only the diagnosis is shown for an invalid proposal
        result.pop("patched", None)
    usage["cost_usd"] = round(usage["cost_usd"], 6)
    if record_event:
        with contextlib.suppress(Exception):  # recording is a convenience
            store.event(
                run_id,
                "repair.proposed",
                result["failed_step"],
                {
                    "valid": result["valid"],
                    "confidence": result["confidence"],
                    "attempts": result["attempts"],
                    **usage,
                },
            )
    return result


def _check(text: str, patch: str, path: str | None) -> list[str]:
    if not patch.strip():
        return ["the proposal has no patch"]
    try:
        patched = apply_patch(text, patch)
    except PatchError as e:
        return [f"the patch does not apply: {e}"]
    if patched == text:
        return ["the patch changes nothing"]
    return validate_patched(patched, path)


def _charge(usage: dict, cfg: LLMSpec, tin: int, tout: int, cost: float | None) -> None:
    usage["llm_calls"] += 1
    usage["input_tokens"] += tin or 0
    usage["output_tokens"] += tout or 0
    usage["cost_usd"] += cost if cost is not None else cfg.cost(tin or 0, tout or 0)


def _over(budget: Any, usage: dict) -> bool:
    tokens = usage["input_tokens"] + usage["output_tokens"]
    return bool(
        (budget.max_tokens and tokens >= budget.max_tokens)
        or (budget.max_usd and usage["cost_usd"] >= budget.max_usd)
    )


# ---------------------------------------------------------------- output
def color_diff(patch: str) -> str:
    out = []
    for line in patch.splitlines():
        if line.startswith(("+++", "---")):
            out.append(f"\033[1m{line}\033[0m")
        elif line.startswith("+"):
            out.append(f"\033[32m{line}\033[0m")
        elif line.startswith("-"):
            out.append(f"\033[31m{line}\033[0m")
        elif line.startswith("@@"):
            out.append(f"\033[36m{line}\033[0m")
        else:
            out.append(line)
    return "\n".join(out)


def text(result: dict, color: bool = False) -> str:
    lines = [f"run {result['run_id']} ({result['pipeline']}): {result['status']}"]
    if result["failed_step"]:
        lines.append(f"failed at step '{result['failed_step']}'")
    if result["diagnosis"]:
        conf = f" (confidence {result['confidence']:.2f})" if result["confidence"] is not None else ""
        lines += ["", f"diagnosis{conf}:", result["diagnosis"].strip()]
    for n in result["notes"]:
        lines.append(f"  - {n}")
    if result["valid"]:
        lines += [
            "",
            "proposed patch (not applied):",
            color_diff(result["patch"]) if color else result["patch"],
        ]
        lines.append("validation: ok (the patched YAML loads and lints without errors)")
    else:
        lines += ["", f"no valid patch after {result['attempts']} attempt(s):"]
        lines += [f"  - {e}" for e in result["errors"]]
    u = result["usage"]
    lines.append(
        f"\nllm: {u['llm_calls']} call(s), {u['input_tokens']} in / {u['output_tokens']} out tokens, "
        f"${u['cost_usd']:.4f}"
    )
    return "\n".join(lines)


def write_patched(result: dict, target: str, pipeline_path: str) -> None:
    """Write the patched YAML to a new file; never over the pipeline file or an existing file."""
    if not result.get("valid"):
        raise ValueError("no valid patch to write")
    if Path(target).resolve() == Path(pipeline_path).resolve():
        raise ValueError("--write must name a new file, not the pipeline itself")
    with open(target, "x", encoding="utf-8") as f:
        f.write(result["patched"])
