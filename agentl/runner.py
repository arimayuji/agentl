"""Runs a pipeline: executes steps in graph order, caches agent calls per
record, emits events to the store, the dashboard and notifications."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import graph, llm, registry
from .notify import Notifier
from .spec import LLMSpec, PipelineSpec, StepSpec, load
from .store import Store, open_store

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
    cost_usd: float = 0.0
    spend: dict[str, dict[str, Any]] = field(default_factory=dict)  # per agent step

    def summary(self, outputs: bool = False) -> dict[str, Any]:
        """JSON-serializable view for task results and logs. Record data only with `outputs=True`."""
        data = asdict(self)
        data["records"] = {k: len(v) for k, v in self.outputs.items()}
        data["rejected"] = {k: len(v) for k, v in self.rejected.items()}
        if not outputs:
            data.pop("outputs")
        return json.loads(json.dumps(data, default=str))


class BudgetExceeded(RuntimeError):
    """The run reached `budget.max_tokens` or `budget.max_usd`."""


def _hash(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


class Runner:
    def __init__(
        self,
        spec: PipelineSpec,
        store: Store | None = None,
        use_cache: bool = True,
        listeners: list[Listener] | None = None,
    ):
        self.spec = spec
        self.store = store or open_store()
        self.use_cache = use_cache
        self.listeners = list(listeners or [])
        self._providers: dict[str, llm.Provider] = {}
        self._spend = threading.Lock()
        self.listeners.append(Notifier(spec.notify, spec.name, summarize=self._summarize))

    # events -------------------------------------------------------------
    def _emit(self, run_id: str, type: str, step: str | None = None, **data: Any) -> None:
        self.store.event(run_id, type, step, data)
        event = {
            "run_id": run_id,
            "pipeline": self.spec.name,
            "ts": time.time(),
            "type": type,
            "step": step,
            "data": data,
        }
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
                json.dumps(events, default=str),
            )
        except Exception as e:
            return f"(summary unavailable: {e})"

    def _record_spend(self, step: StepSpec, before: tuple[int, int, float], result: RunResult) -> None:
        if step.kind != "agent":
            return
        result.spend[step.id] = {
            "input_tokens": result.input_tokens - before[0],
            "output_tokens": result.output_tokens - before[1],
            "cost_usd": round(result.cost_usd - before[2], 6),
        }

    # budget -------------------------------------------------------------
    def _charge(self, result: RunResult, spec: LLMSpec, input_tokens: int, output_tokens: int) -> None:
        with self._spend:
            result.input_tokens += input_tokens
            result.output_tokens += output_tokens
            result.cost_usd += spec.cost(input_tokens, output_tokens)

    def _check_budget(self, result: RunResult) -> None:
        with self._spend:
            reason = self._over_budget(result)
        if reason:
            raise BudgetExceeded(reason)

    def _over_budget(self, result: RunResult) -> str | None:
        b = self.spec.budget
        if b is None:
            return None
        if b.max_tokens is not None and result.input_tokens + result.output_tokens >= b.max_tokens:
            return f"max_tokens {b.max_tokens} reached ({result.input_tokens + result.output_tokens} used)"
        if b.max_usd is not None and result.cost_usd >= b.max_usd:
            return f"max_usd {b.max_usd} reached ({result.cost_usd:.4f} spent)"
        return None

    # steps --------------------------------------------------------------
    def _run_agent(self, run_id: str, step: StepSpec, records: Records, result: RunResult) -> Records:
        schema = llm.schema_model(f"{step.id}_output", step.output or {})
        prov = self._provider(step.llm)
        llm_spec = self.spec.llm[step.llm]
        cacheable = self.use_cache and step.cached

        def one(rec: dict) -> tuple[dict | None, dict | None]:
            key = _hash("agent", step.agent, step.output, llm_spec.model_dump(exclude={"price"}), rec)
            if cacheable and (hit := self.store.cache_get(key)) is not None:
                return {**rec, **hit}, None
            last: Exception | None = None
            for _ in range(2):  # one retry on invalid output
                self._check_budget(result)
                try:
                    res = prov.structured(step.agent or "", rec, schema)
                except (ValidationError, ValueError, RuntimeError) as e:
                    last = e
                    continue
                self._charge(result, llm_spec, res.input_tokens, res.output_tokens)
                out = res.value.model_dump()
                if cacheable:
                    self.store.cache_set(key, out)
                return {**rec, **out}, None
            return None, {**rec, "_error": str(last)}

        ok: Records = []
        bad: Records = []
        with ThreadPoolExecutor(max_workers=max(1, step.concurrency)) as pool:
            for good, rejected in pool.map(one, records):
                if good is not None:
                    ok.append(good)
                elif rejected is not None:
                    bad.append(rejected)
        if bad:
            result.rejected[step.id] = bad
            self._emit(
                run_id,
                "item.rejected",
                step.id,
                count=len(bad),
                total=len(records),
                rate=round(len(bad) / max(1, len(records)), 4),
                error=bad[0]["_error"],
            )
        return ok

    def _run_step(
        self, run_id: str, step: StepSpec, outputs: dict[str, Records], result: RunResult
    ) -> Records:
        refs = step.inputs
        data: Any = None
        if len(refs) == 1:
            data = outputs[refs[0]]
        elif refs:
            data = {r: outputs[r] for r in refs}

        if step.kind == "agent":
            if isinstance(data, dict):
                raise ValueError("agent steps take a single input; join the inputs first")
            # A source agent step (no input) runs once; an empty upstream means nothing to do.
            return self._run_agent(run_id, step, [{}] if data is None else data, result)

        fn = registry.get(step.use) if step.use else registry.resolve_fn(step.fn or "", self.spec.base_dir)
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
                before = (result.input_tokens, result.output_tokens, result.cost_usd)
                try:
                    out = self._run_step(run_id, step, result.outputs, result)
                except Exception as e:
                    self._record_spend(step, before, result)
                    if isinstance(e, BudgetExceeded):
                        self._emit(
                            run_id,
                            "budget.exceeded",
                            step_id,
                            reason=str(e),
                            input_tokens=result.input_tokens,
                            output_tokens=result.output_tokens,
                            cost_usd=round(result.cost_usd, 6),
                        )
                    self._emit(
                        run_id,
                        "step.failed",
                        step_id,
                        error=f"{type(e).__name__}: {e}",
                        **result.spend.get(step_id, {}),
                    )
                    raise
                result.outputs[step_id] = out
                self._record_spend(step, before, result)
                self._emit(
                    run_id,
                    "step.succeeded",
                    step_id,
                    records=len(out),
                    seconds=round(time.time() - t0, 3),
                    **result.spend.get(step_id, {}),
                )
                if not out and not step.inputs:
                    self._emit(run_id, "source.empty", step_id)
            result.status = "succeeded"
        except Exception as e:
            result.status = "failed"
            result.error = f"{type(e).__name__}: {e}"
        self.store.finish_run(run_id, result.status, result.error)
        self._emit(
            run_id,
            f"run.{result.status}",
            seconds=round(time.time() - started, 3),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=round(result.cost_usd, 6),
            rejected=sum(len(v) for v in result.rejected.values()),
            **({"error": result.error} if result.error else {}),
        )
        return result


class PipelineFailed(RuntimeError):
    """Raised by `run(..., raise_on_failure=True)`; carries the `RunResult`."""

    def __init__(self, result: RunResult | str):
        # Orchestrators rebuild exceptions from their message, so a plain string must work too.
        if isinstance(result, str):
            super().__init__(result)
            self.result: RunResult | None = None
        else:
            super().__init__(f"run {result.run_id} failed: {result.error}")
            self.result = result

    def __reduce__(self) -> tuple[Any, ...]:
        return (PipelineFailed, (str(self),))


def run(
    pipeline: PipelineSpec | str | os.PathLike,
    *,
    vars: dict[str, str] | None = None,
    db: str | os.PathLike | None = None,
    raise_on_failure: bool = False,
    **kwargs: Any,
) -> RunResult:
    """Run a pipeline from a spec or a YAML path. The stable entry point for schedulers
    and orchestrators (cron, Celery, Temporal, Airflow, Prefect, Lambda).

    `vars` override environment variables for `${VAR}` interpolation. `db` is the run
    store path (default `.agentl/agentl.db`). With `raise_on_failure`, a failed run raises
    `PipelineFailed`, which is what most orchestrators need to trigger their retries."""
    if isinstance(pipeline, PipelineSpec):
        spec = pipeline
    else:
        spec = load(Path(pipeline), {**os.environ, **(vars or {})})
    if "store" in kwargs:
        result = Runner(spec, **kwargs).run()
    else:
        with open_store(db) as store:
            result = Runner(spec, store=store, **kwargs).run()
    if raise_on_failure and result.status != "succeeded":
        raise PipelineFailed(result)
    return result
