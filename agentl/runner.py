"""Runs a pipeline: executes steps in graph order, caches agent calls per
record, emits events to the store, the dashboard and notifications."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import ValidationError

from . import graph, llm, registry
from .notify import Notifier
from .spec import PipelineSpec, StepSpec
from .store import Store

Records = list[dict[str, Any]]
Listener = Callable[[dict], None]


@dataclass
class RunResult:
    run_id: str
    status: str
    outputs: dict[str, Records] = field(default_factory=dict)
    rejected: dict[str, Records] = field(default_factory=dict)
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


def _hash(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


class Runner:
    def __init__(self, spec: PipelineSpec, store: Store | None = None, use_cache: bool = True,
                 listeners: list[Listener] | None = None):
        self.spec = spec
        self.store = store or Store()
        self.use_cache = use_cache
        self.listeners = list(listeners or [])
        self._providers: dict[str, llm.Provider] = {}
        self.listeners.append(Notifier(spec.notify, spec.name, summarize=self._summarize))

    # events -------------------------------------------------------------
    def _emit(self, run_id: str, type: str, step: str | None = None, **data: Any) -> None:
        self.store.event(run_id, type, step, data)
        event = {"run_id": run_id, "type": type, "step": step, "data": data}
        for listener in self.listeners:
            listener(event)

    # llm ----------------------------------------------------------------
    def _provider(self, name: str) -> llm.Provider:
        if name not in self._providers:
            self._providers[name] = llm.provider(self.spec.llm[name])
        return self._providers[name]

    def _summarize(self, event: dict) -> str:
        try:
            events = self.store.events(event["run_id"])[-30:]
            return self._provider("default").text(
                "Summarize this data pipeline run in at most 3 short lines for the person "
                "on call. Say what failed and the most likely cause, if any.",
                json.dumps(events, default=str))
        except Exception as e:
            return f"(summary unavailable: {e})"

    # steps --------------------------------------------------------------
    def _run_agent(self, run_id: str, step: StepSpec, records: Records, result: RunResult) -> Records:
        schema = llm.schema_model(f"{step.id}_output", step.output or {})
        prov = self._provider(step.llm)
        llm_spec = self.spec.llm[step.llm]
        cacheable = self.use_cache and step.cached

        def one(rec: dict) -> tuple[dict | None, dict | None, int, int]:
            key = _hash("agent", step.agent, step.output, llm_spec.model_dump(), rec)
            if cacheable and (hit := self.store.cache_get(key)) is not None:
                return {**rec, **hit}, None, 0, 0
            last: Exception | None = None
            for _ in range(2):  # one retry on invalid output
                try:
                    res = prov.structured(step.agent or "", rec, schema)
                    out = res.value.model_dump()
                    if cacheable:
                        self.store.cache_set(key, out)
                    return {**rec, **out}, None, res.input_tokens, res.output_tokens
                except (ValidationError, ValueError, RuntimeError) as e:
                    last = e
            return None, {**rec, "_error": str(last)}, 0, 0

        ok: Records = []
        bad: Records = []
        with ThreadPoolExecutor(max_workers=max(1, step.concurrency)) as pool:
            for good, rejected, tin, tout in pool.map(one, records):
                result.input_tokens += tin
                result.output_tokens += tout
                if good is not None:
                    ok.append(good)
                else:
                    bad.append(rejected)
        if bad:
            result.rejected[step.id] = bad
            self._emit(run_id, "item.rejected", step.id, count=len(bad), total=len(records),
                       rate=round(len(bad) / max(1, len(records)), 4), error=bad[0]["_error"])
        return ok

    def _run_step(self, run_id: str, step: StepSpec, outputs: dict[str, Records], result: RunResult) -> Records:
        refs = step.inputs
        data: Any = None
        if len(refs) == 1:
            data = outputs[refs[0]]
        elif refs:
            data = {r: outputs[r] for r in refs}

        if step.kind == "agent":
            if isinstance(data, dict):
                raise ValueError("agent steps take a single input; join the inputs first")
            return self._run_agent(run_id, step, data or [{}], result)

        fn = registry.get(step.use) if step.use else registry.resolve_fn(step.fn or "")
        key = _hash("step", step.model_dump(by_alias=True), data)
        if self.use_cache and step.cached and (hit := self.store.cache_get(key)) is not None:
            return hit
        out = fn(data, **step.with_)
        out = list(out) if out is not None else []
        if self.use_cache and step.cached:
            self.store.cache_set(key, out)
        return out

    # run ----------------------------------------------------------------
    def run(self) -> RunResult:
        run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        result = RunResult(run_id=run_id, status="running")
        self.store.start_run(run_id, self.spec.name, graph.mermaid(self.spec))
        self._emit(run_id, "run.started", steps=len(self.spec.steps))
        started = time.time()
        try:
            for step_id in graph.order(self.spec):
                step = self.spec.step(step_id)
                self._emit(run_id, "step.started", step_id, kind=step.kind)
                t0 = time.time()
                try:
                    out = self._run_step(run_id, step, result.outputs, result)
                except Exception as e:
                    self._emit(run_id, "step.failed", step_id, error=f"{type(e).__name__}: {e}")
                    raise
                result.outputs[step_id] = out
                self._emit(run_id, "step.succeeded", step_id, records=len(out),
                           seconds=round(time.time() - t0, 3))
                if not out and not step.inputs:
                    self._emit(run_id, "source.empty", step_id)
            result.status = "succeeded"
        except Exception as e:
            result.status = "failed"
            result.error = f"{type(e).__name__}: {e}"
        self.store.finish_run(run_id, result.status, result.error)
        self._emit(run_id, f"run.{result.status}", seconds=round(time.time() - started, 3),
                   input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                   rejected=sum(len(v) for v in result.rejected.values()),
                   **({"error": result.error} if result.error else {}))
        return result


def run(spec: PipelineSpec, **kwargs: Any) -> RunResult:
    return Runner(spec, **kwargs).run()
