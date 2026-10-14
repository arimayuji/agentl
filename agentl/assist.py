"""Helpers behind `agentl new`, `agentl doctor` and the MCP server.

`new` asks a model for a pipeline YAML, validates it and feeds any error back for another try.
`doctor` explains a failed run from what the store recorded; the model, when there is one, only
adds a suggested fix on top of that explanation."""

from __future__ import annotations

import inspect
import os
import re
from typing import Any

import yaml

from . import graph, registry
from .spec import LLMSpec, PipelineSpec, SpecError, parse


# ---------------------------------------------------------------- validation
def check(spec: PipelineSpec) -> None:
    """What `agentl validate` checks: the step graph and that every connector is installed."""
    graph.order(spec)
    for s in spec.steps:
        if s.use:
            registry.get(s.use)


def validate_text(text: str) -> PipelineSpec:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise SpecError(f"invalid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise SpecError("a pipeline is a YAML mapping with name and steps")
    spec = parse(raw)
    check(spec)
    return spec


# ---------------------------------------------------------------- model
def llm_from_env(provider: str | None = None, model: str | None = None) -> Any:
    """A provider for the assistant commands: flags first, then AGENTL_PROVIDER / AGENTL_MODEL."""
    from .llm import provider as make

    spec = LLMSpec(
        provider=provider or os.environ.get("AGENTL_PROVIDER") or "anthropic",
        model=model or os.environ.get("AGENTL_MODEL") or LLMSpec().model,
        max_tokens=8192,
    )
    return make(spec)


def catalog() -> str:
    """One line per installed connector: name and parameters, for the model to pick from."""
    lines = []
    for name in registry.names():
        try:
            params = list(inspect.signature(registry.get(name)).parameters)
        except (TypeError, ValueError):
            params = []
        params = [p for p in params if p not in ("input", "kwargs")]
        lines.append(f"- {name}({', '.join(params)})")
    return "\n".join(lines)


NEW_INSTRUCTION = """You write agentl pipelines: YAML with `name` and a list of `steps`.
Each step has an `id` and one of:
- `use: <connector>` with its parameters under `with:`
- `agent: <instruction>` with `output: {field: type}` for an LLM step over each record
  (types: str, int, float, bool, list[str])
- `check: <step id>` with `expect:` (rows, not_null, unique, accepted, range, fresh, vs_last_run)
- `fn: module:function` for Python code
`input: <step id>` (or a list of ids) wires steps. Put secrets in environment variables as ${VAR}.
Pipelines with agent steps need `llm: {provider: anthropic, model: claude-opus-5-5}`.
Only use these connectors:
{catalog}

Answer with the YAML only, in one ```yaml block, no explanation."""

_BLOCK = re.compile(r"```(?:ya?ml)?\s*\n(.*?)```", re.S)


def _yaml_of(answer: str) -> str:
    m = _BLOCK.search(answer)
    return (m.group(1) if m else answer).strip() + "\n"


def new_pipeline(description: str, llm: Any, attempts: int = 3) -> tuple[str, PipelineSpec]:
    """A validated pipeline YAML for `description`. Validation errors go back to the model."""
    instruction = NEW_INSTRUCTION.replace("{catalog}", catalog())
    prompt, last = description, ""
    for _ in range(attempts):
        text = _yaml_of(llm.text(instruction, prompt))
        try:
            return text, validate_text(text)
        except (SpecError, KeyError, ValueError) as e:
            last = str(e)
            prompt = (
                f"{description}\n\nYour previous answer:\n```yaml\n{text}```\n"
                f"failed validation with:\n{last}\nFix it and answer with the whole YAML again."
            )
    raise SpecError(f"no valid pipeline after {attempts} attempts; last error: {last}")


# ---------------------------------------------------------------- doctor
def failures(store: Any, limit: int = 20) -> list[dict]:
    return [r for r in store.runs(limit * 5) if r["status"] == "failed"][:limit]


def diagnose(store: Any, run_id: str) -> dict:
    """What the store knows about a run: status, the failing step, its error, the failed records."""
    run = store.run(run_id)
    if run is None:
        raise KeyError(f"no run '{run_id}' in this store")
    events = store.events(run_id)
    failed = [e for e in events if e["type"] in ("step.failed", "check.failed", "budget.exceeded")]
    first = failed[0] if failed else None
    records = [e for e in events if e["type"] == "item.rejected"][:5]
    return {
        "run_id": run_id,
        "pipeline": run["pipeline"],
        "status": run["status"],
        "error": run.get("error"),
        "failed_step": first["step"] if first else None,
        "failure": first["data"] if first else None,
        "rejected": [{"step": e["step"], **e["data"]} for e in records],
        "steps_done": [e["step"] for e in events if e["type"] == "step.succeeded"],
    }


DOCTOR_INSTRUCTION = """You help fix failed agentl pipeline runs. You get what the run store
recorded. In at most 6 short lines: the likely cause, then the concrete fix (a YAML change, an
environment variable, a permission). Say when the record is not enough to tell."""


def explain(report: dict, llm: Any | None = None, pipeline_yaml: str | None = None) -> str:
    lines = [f"run {report['run_id']} ({report['pipeline']}): {report['status']}"]
    if report["status"] != "failed":
        return lines[0]
    if report["failed_step"]:
        lines.append(f"failed at step '{report['failed_step']}'")
    if report["error"]:
        lines.append(f"error: {report['error']}")
    if report["failure"] and report["failure"].get("findings"):
        lines += [f"  - {f['check']}: {f['detail']}" for f in report["failure"]["findings"]]
    for r in report["rejected"]:
        lines.append(f"  {r.get('count')} record(s) rejected in {r.get('total')}: {r.get('error')}")
    if report["steps_done"]:
        lines.append(f"steps that finished: {', '.join(report['steps_done'])}")
    text = "\n".join(lines)
    if llm is None:
        return text
    content = text + (f"\n\nPipeline:\n{pipeline_yaml}" if pipeline_yaml else "")
    return text + "\n\nsuggestion:\n" + llm.text(DOCTOR_INSTRUCTION, content).strip()
