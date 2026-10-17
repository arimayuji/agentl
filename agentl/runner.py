"""Runs a pipeline: executes steps in graph order, caches agent calls per
record, emits events to the store, the dashboard and notifications."""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, create_model

from . import checks, contracts, graph, llm, registry, waits
from .notify import Notifier
from .spec import LLMSpec, PipelineSpec, StepSpec, load
from .store import Store, open_store
from .tools import ToolBox, api_name

Records = list[dict[str, Any]]
Listener = Callable[[dict], None]


@dataclass
class RunResult:
    run_id: str
    status: str
    outputs: dict[str, Records] = field(default_factory=lambda: Outputs())
    rejected: dict[str, Records] = field(default_factory=dict)
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0  # part of input_tokens served from provider prompt caches
    llm_calls: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    spend: dict[str, dict[str, Any]] = field(default_factory=dict)  # per agent step
    # per upsert step: {"inserted": n, "updated": n, "unchanged": n}
    writes: dict[str, dict[str, int]] = field(default_factory=dict)

    def summary(self, outputs: bool = False) -> dict[str, Any]:
        """JSON-serializable view for task results and logs. Record data only with `outputs=True`."""
        data = asdict(self)
        data["records"] = {k: len(v) for k, v in self.outputs.items()}
        data["rejected"] = {k: len(v) for k, v in self.rejected.items()}
        if not outputs:
            data.pop("outputs")
        return json.loads(json.dumps(data, default=str))


SAMPLE_ROWS = 5
UPSERT_COUNTS = ("inserted", "updated", "unchanged")
SAMPLE_CHARS = 200


def _clip(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, default=str, ensure_ascii=False)
        return value if len(text) <= SAMPLE_CHARS else text[:SAMPLE_CHARS] + "…"
    if isinstance(value, str) and len(value) > SAMPLE_CHARS:
        return value[:SAMPLE_CHARS] + "…"
    return value


def _mask_values(value: Any, hidden: set[str]) -> Any:
    """Secret values replaced by *** anywhere in strings, keys included, before any clipping."""
    from . import secrets

    if isinstance(value, str):
        return secrets.mask(value, hidden)
    if isinstance(value, dict):
        return {_mask_values(k, hidden): _mask_values(v, hidden) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_values(v, hidden) for v in value]
    return value


def _clip_record(record: Any) -> Any:
    return {k: _clip(v) for k, v in record.items()} if isinstance(record, dict) else _clip(record)


def _is_table(value: Any) -> bool:
    return hasattr(value, "num_rows") and hasattr(value, "to_pylist")


class Outputs(dict):
    """Step outputs. With the Arrow fast path a step's output can be a pyarrow Table internally;
    every way of reading it (indexing, iteration, `dict(...)`, `{**...}`, `json.dumps`, `repr`,
    copy and pickle) gives records, converted once on first access."""

    def __init__(self, *args: Any, **kw: Any):
        super().__init__(*args, **kw)
        self._lock = threading.Lock()

    def raw(self, key: str) -> Any:
        return super().__getitem__(key)

    def __getitem__(self, key: str) -> Records:
        value = super().__getitem__(key)
        if _is_table(value):
            with self._lock:
                value = super().__getitem__(key)
                if _is_table(value):
                    value = value.to_pylist()
                    super().__setitem__(key, value)
        return value

    def __iter__(self) -> Any:
        # Overriding __iter__ makes dict(...) and {**...} go through keys() and __getitem__
        # instead of copying the raw values.
        return iter(list(super().keys()))

    def __reduce__(self) -> Any:
        # copy and pickle: plain records, without the lock
        return (Outputs, (dict(self.items()),))

    def __repr__(self) -> str:
        return repr(dict(self.items()))

    def get(self, key: str, default: Any = None) -> Any:
        return self[key] if key in self else default  # noqa: SIM401 (self.get would recurse)

    def values(self) -> Any:
        return [self[k] for k in self]

    def items(self) -> Any:
        return [(k, self[k]) for k in self]

    def __eq__(self, other: object) -> bool:
        return dict(self.items()) == other

    __hash__ = None

    def copy(self) -> Outputs:
        return Outputs(self.items())


class CheckFailed(RuntimeError):
    """A check step with `on_fail: stop` found problems; the data was not passed on."""


class WaitTimeout(RuntimeError):
    """A `wait_until` step with `on_timeout: fail` ran out of time before its condition held."""


class BudgetExceeded(RuntimeError):
    """The run reached `budget.max_tokens` or `budget.max_usd`."""


def agent_cache_key(spec: PipelineSpec, step: StepSpec, rec: dict) -> str:
    """The per-record cache key of an agent step (also used by `agentl plan` to count hits)."""
    # Without a cascade the key stays what it was, so existing caches keep their hits.
    llm_key: Any = spec.llm[step.llm].model_dump(exclude={"price"})
    if step.escalate:
        llm_key = [spec.llm[n].model_dump(exclude={"price"}) for n in [step.llm, *step.escalate]]
    # records_per_call is not part of the key: the answer per record is the same.
    if step.tools:  # tools can change the answer, so they are part of the key
        return _hash("agent", step.agent, step.output, llm_key, step.tools, rec)
    return _hash("agent", step.agent, step.output, llm_key, rec)


def _describe(e: BaseException) -> str:
    text = f"{type(e).__name__}: {e}"
    if "CERTIFICATE_VERIFY_FAILED" in text:  # netguard is not imported up front: it is heavy
        from .netguard import describe

        return describe(e)
    return text


def _hash(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


class Runner:
    def __init__(
        self,
        spec: PipelineSpec,
        store: Store | None = None,
        use_cache: bool = True,
        listeners: list[Listener] | None = None,
        trigger: list[dict] | None = None,
        replay: dict[str, Records] | None = None,
    ):
        self.spec = spec
        # step id -> records that step runs on instead of its inputs (retry of rejected records)
        self.replay = dict(replay or {})
        self.trigger = list(trigger or [])  # what started this run; a `use: trigger` step reads it
        self.store = store or open_store()
        self.use_cache = use_cache
        self.listeners = list(listeners or [])
        self._providers: dict[str, llm.Provider] = {}
        self._toolbox: ToolBox | None = None
        self._spend = threading.Lock()
        self._events = threading.Lock()
        self._provider_lock = threading.Lock()
        self._arrow_out: set[str] = set()
        self._reject_stats: dict[str, list[int]] = {}  # step id -> [rejected, seen], for checks
        self._polars_fed: set[str] = set()
        self._run_id: str | None = None  # for events raised from inside providers (llm.fallback)
        self._cursor_start: dict[str, Any] = {}  # step id -> cursor this run started from
        self._cursor_new: dict[str, Any] = {}  # step id -> largest value seen this run
        self._after_run: list = []  # (on_success, on_failure) from connectors, see registry.after_run
        self.listeners.append(Notifier(spec.notify, spec.name, summarize=self._summarize))

    # events -------------------------------------------------------------
    def _emit(self, run_id: str, type: str, step: str | None = None, **data: Any) -> None:
        event = {
            "run_id": run_id,
            "pipeline": self.spec.name,
            "ts": time.time(),
            "type": type,
            "step": step,
            "data": data,
        }
        with self._events:  # steps may run in parallel: one event at a time, store and listeners
            self.store.event(run_id, type, step, data)
            for listener in self.listeners:
                listener(event)

    # llm ----------------------------------------------------------------
    def _provider(self, name: str) -> llm.Provider:
        with self._provider_lock:
            if name not in self._providers:
                spec = self.spec.llm[name]
                prov = llm.provider(spec)
                if spec.fallback:
                    chain = [(name, spec, prov)]
                    chain += [(f, self.spec.llm[f], llm.provider(self.spec.llm[f])) for f in spec.fallback]
                    prov = llm.FallbackProvider(chain)
                    prov.on_fallback = self._on_fallback
                self._providers[name] = prov
            return self._providers[name]

    def _on_fallback(self, failed: str, to: str, error: Exception) -> None:
        if self._run_id:
            reason = f"{type(error).__name__}: {error}"[:300]
            self._emit(self._run_id, "llm.fallback", llm=failed, to=to, error=reason)

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

    def _step_spend(self, step: StepSpec, result: RunResult) -> dict[str, Any]:
        """The step's spend, with cost rounded for events. Empty for non-agent steps."""
        with self._spend:
            spend = result.spend.get(step.id)
            if spend is None:
                return {}
            spend["cost_usd"] = round(spend["cost_usd"], 6)
            return dict(spend)

    # budget -------------------------------------------------------------
    def _charge(
        self, result: RunResult, step: StepSpec, spec: LLMSpec, usage: Any, batch: bool = False
    ) -> None:
        """`usage` is an `llm.Result` or an exception carrying the tokens of a failed call. Counted
        for the run and for the step, so spend per step stays right when steps run in parallel."""
        tin, tout = getattr(usage, "input_tokens", 0), getattr(usage, "output_tokens", 0)
        read, write = getattr(usage, "cache_read_tokens", 0), getattr(usage, "cache_write_tokens", 0)
        known = getattr(usage, "cost_usd", None)
        cost = known if known is not None else spec.cost(tin, tout, read, write)
        if batch and spec.price and known is None:
            cost *= spec.price.batch
        with self._spend:
            result.input_tokens += tin
            result.output_tokens += tout
            result.cache_read_tokens += read
            result.llm_calls += 1
            result.cost_usd += cost
            mine = result.spend[step.id]
            mine["input_tokens"] += tin
            mine["output_tokens"] += tout
            mine["cache_read_tokens"] += read
            mine["llm_calls"] += 1
            mine["cost_usd"] += cost

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
        if b.max_tool_calls is not None and result.tool_calls >= b.max_tool_calls:
            return f"max_tool_calls {b.max_tool_calls} reached"
        return None

    # tools --------------------------------------------------------------
    def _tool_caller(self, run_id: str, step: StepSpec, result: RunResult) -> tuple[list[dict], llm.ToolCall]:
        """Tool definitions for the provider and the function it calls for each tool use: it
        checks the budget, runs the tool, records spend and emits `tool.called`. A tool that fails
        answers `error: ...` to the model instead of failing the record."""
        with self._provider_lock:  # steps may run in parallel
            if self._toolbox is None:
                self._toolbox = ToolBox(self.spec)
            toolbox = self._toolbox
        by_name = {api_name(t.name): t for t in toolbox.for_step(step)}
        defs = [
            {"name": n, "description": t.description, "input_schema": t.input_schema}
            for n, t in by_name.items()
        ]

        def call(name: str, args: dict) -> str:
            t = by_name.get(name)
            if t is None:
                return f"error: unknown tool {name}"
            self._check_budget(result)
            with self._spend:
                result.tool_calls += 1
                result.spend[step.id]["tool_calls"] += 1
            t0 = time.time()
            try:
                out, ok = t.run(args), True
            except Exception as e:
                out, ok = f"error: {type(e).__name__}: {e}", False
            ok = ok and not out.startswith("error:")
            shown = json.dumps(args, ensure_ascii=False, default=str)
            self._emit(
                run_id,
                "tool.called",
                step.id,
                tool=t.name,
                ok=ok,
                seconds=round(time.time() - t0, 3),
                args=shown if len(shown) <= 300 else shown[:300] + "…",
                **({} if ok else {"error": out[:300]}),
            )
            return out

        return defs, call

    # batch mode ---------------------------------------------------------
    def _run_batch(
        self,
        run_id: str,
        step: StepSpec,
        prov: Any,
        schema: type[BaseModel],
        recs: Records,
        key: Callable[[dict], str],
        result: RunResult,
    ) -> list[llm.Result | Exception]:
        """Submits one provider batch for `recs`, waits for it and returns one answer per record.
        The batch id is kept in the store, so a run that stops while waiting picks the same batch
        up again instead of paying for a new one."""
        if not hasattr(prov, "submit_batch"):
            raise ValueError(
                f"step '{step.id}': llm provider '{self.spec.llm[step.llm].provider}' has no batch mode"
            )
        llm_spec = self.spec.llm[step.llm]
        ids = {f"r{n}": rec for n, rec in enumerate(recs)}
        resume = self.use_cache and step.cached
        ticket = _hash("batch", [key(r) for r in recs])
        saved = self.store.cache_get(ticket) if resume else None
        if saved:
            batch_id = saved["batch_id"]
            self._emit(run_id, "batch.resumed", step.id, batch_id=batch_id, requests=len(ids))
        else:
            self._check_budget(result)
            batch_id = prov.submit_batch(step.agent or "", ids, schema)
            if resume:
                self.store.cache_set(ticket, {"batch_id": batch_id})
            self._emit(run_id, "batch.submitted", step.id, batch_id=batch_id, requests=len(ids))
        t0 = time.time()
        while not prov.batch_done(batch_id):
            if time.time() - t0 > step.batch_timeout_seconds:
                raise TimeoutError(
                    f"batch {batch_id} not finished after {step.batch_timeout_seconds:.0f}s; "
                    "run again later to pick it up"
                )
            time.sleep(step.batch_poll_seconds)
        answers = prov.batch_results(batch_id, schema)
        for res in answers.values():
            self._charge(result, step, llm_spec, res, batch=True)
        out = [answers.get(cid, RuntimeError("missing from batch results")) for cid in ids]
        ok = sum(isinstance(r, llm.Result) for r in out)
        self._emit(
            run_id,
            "batch.ended",
            step.id,
            batch_id=batch_id,
            succeeded=ok,
            failed=len(out) - ok,
            seconds=round(time.time() - t0, 3),
        )
        return out

    # steps --------------------------------------------------------------
    def _run_agent(self, run_id: str, step: StepSpec, records: Records, result: RunResult) -> Records:
        schema = llm.schema_model(f"{step.id}_output", step.output or {})
        prov = self._provider(step.llm)
        llm_spec = self.spec.llm[step.llm]
        cacheable = self.use_cache and step.cached
        chain = [step.llm, *step.escalate]

        tool_defs: list[dict] = []
        call_tool: llm.ToolCall | None = None
        if step.tools:
            for name in chain:
                if not hasattr(self._provider(name), "structured_tools"):
                    provider = self.spec.llm[name].provider
                    raise ValueError(f"step '{step.id}': llm provider '{provider}' does not support tools")
            tool_defs, call_tool = self._tool_caller(run_id, step, result)

        def key(rec: dict) -> str:
            return agent_cache_key(self.spec, step, rec)

        def done(rec: dict, out: dict, by: str = step.llm) -> tuple[dict, None]:
            if cacheable:
                self.store.cache_set(key(rec), out)
            with self._spend:
                answered = result.spend[step.id].setdefault("answered_by", {})
                answered[by] = answered.get(by, 0) + 1
            return {**rec, **out}, None

        def unsure(out: dict) -> bool:
            conf = out.get("confidence")
            return (
                step.escalate_below is not None
                and isinstance(conf, int | float)
                and conf < step.escalate_below
            )

        def one(rec: dict) -> tuple[dict | None, dict | None]:
            last: Exception | None = None
            for n, name in enumerate(chain):
                p, spec = self._provider(name), self.spec.llm[name]
                for _ in range(2):  # one retry on invalid output
                    self._check_budget(result)
                    try:
                        if call_tool is not None:
                            res = p.structured_tools(  # type: ignore[attr-defined]
                                step.agent or "", rec, schema, tool_defs, call_tool, step.max_tool_calls
                            )
                        else:
                            res = p.structured(step.agent or "", rec, schema)
                    except BudgetExceeded:
                        raise
                    except (ValidationError, ValueError, RuntimeError) as e:
                        # Invalid answers still cost tokens: count them for budget and spend.
                        self._charge(result, step, spec, e)
                        last = e
                        continue
                    self._charge(result, step, spec, res)
                    out = res.value.model_dump()
                    if n + 1 < len(chain) and unsure(out):
                        last = ValueError(f"confidence {out['confidence']} below {step.escalate_below}")
                        break  # the next llm answers this record
                    return done(rec, out, name)
            return None, {**rec, "_error": str(last)}

        batch_schema = _batch_schema(step.id, schema)

        def many(group: list[dict]) -> list[tuple[dict | None, dict | None]]:
            """One call for the whole group. Items that come back missing, duplicated or invalid
            fall back to one call per record, so a bad item never drops its neighbours."""
            outs: dict[int, dict] = {}
            self._check_budget(result)
            payload = {"records": [{"index": i, "record": r} for i, r in enumerate(group)]}
            try:
                res = prov.structured(_batch_instruction(step.agent or "", len(group)), payload, batch_schema)
            except (ValidationError, ValueError, RuntimeError) as e:
                self._charge(result, step, llm_spec, e)
            else:
                self._charge(result, step, llm_spec, res)
                seen: set[int] = set()
                for item in res.value.model_dump()["items"]:
                    i = item.pop("index", None)
                    if not isinstance(i, int) or i in seen or not 0 <= i < len(group):
                        outs.pop(i, None)
                        continue
                    seen.add(i)
                    with contextlib.suppress(ValidationError):
                        outs[i] = schema.model_validate(item).model_dump()
            return [done(r, outs[i]) if i in outs else one(r) for i, r in enumerate(group)]

        slots: list[tuple[dict | None, dict | None] | None] = [None] * len(records)
        todo: list[int] = []
        first: dict[str, int] = {}  # identical records are asked once per run
        copies: list[tuple[int, int]] = []
        for i, rec in enumerate(records):
            k = key(rec)
            if k in first:
                copies.append((i, first[k]))
            elif cacheable and (hit := self.store.cache_get(k)) is not None:
                slots[i] = ({**rec, **hit}, None)
            else:
                first[k] = i
                todo.append(i)
        size = step.records_per_call
        groups = [todo[n : n + size] for n in range(0, len(todo), size)]
        if step.mode == "batch" and todo:
            answers_by_slot = self._run_batch(
                run_id, step, prov, schema, [records[i] for i in todo], key, result
            )
            failed = []
            for i, res in zip(todo, answers_by_slot, strict=True):
                if isinstance(res, llm.Result):
                    slots[i] = done(records[i], res.value.model_dump())
                else:
                    failed.append(i)
            # Requests the batch could not answer are retried as normal calls.
            todo, groups = failed, [[i] for i in failed]
        with ThreadPoolExecutor(max_workers=max(1, step.concurrency)) as pool:
            if size == 1:
                answers = pool.map(lambda g: [one(records[g[0]])], groups)
            else:
                answers = pool.map(lambda g: many([records[i] for i in g]), groups)
            for g, answer in zip(groups, answers, strict=True):
                for i, a in zip(g, answer, strict=True):
                    slots[i] = a
        for i, src in copies:
            slots[i] = slots[src]
        if copies:
            self._emit(run_id, "agent.deduplicated", step.id, records=len(copies))
        ok: Records = []
        bad: Records = []
        for slot in slots:
            assert slot is not None
            good, rejected = slot
            if good is not None:
                ok.append(good)
            elif rejected is not None:
                bad.append(rejected)
        self._rejects(run_id, step.id, bad, len(records), result)
        return ok

    def _rejects(self, run_id: str, step_id: str, bad: Records, total: int, result: RunResult) -> None:
        """Rejected records of a step: kept on the result and in the quarantine, counted for
        `rejected_rate` checks, and announced with an `item.rejected` event. Chunked steps call
        this once per chunk."""
        with self._spend:
            stats = self._reject_stats.setdefault(step_id, [0, 0])
            stats[0] += len(bad)
            stats[1] += total
        if not bad:
            return
        result.rejected.setdefault(step_id, []).extend(bad)
        self._quarantine(run_id, step_id, bad)
        self._emit(
            run_id,
            "item.rejected",
            step_id,
            count=len(bad),
            total=total,
            rate=round(len(bad) / max(1, total), 4),
            error=bad[0].get("_error"),
        )

    def _contract(self, run_id: str, step: StepSpec, out: Any, result: RunResult, start: int = 0) -> Records:
        """Applies the step's `schema:` to its output (#122). Arrow tables become records."""
        rows = out.to_pylist() if _is_table(out) else list(out or [])
        if step.schema_ is None:
            return rows
        good, bad = contracts.validate(
            rows, step.schema_, on_bad=step.on_bad, extra=step.extra, step=step.id, start=start
        )
        self._rejects(run_id, step.id, bad, len(rows), result)
        return good

    def _write_rejected(self, step: StepSpec, result: RunResult) -> None:
        """`rejected_to`: hands all the step's rejected records of this run to a writer."""
        rows = result.rejected.get(step.id)
        if step.rejected_to is None or not rows:
            return
        registry.get(step.rejected_to.use)(list(rows), **step.rejected_to.with_)

    def _run_step(
        self, run_id: str, step: StepSpec, outputs: dict[str, Records], result: RunResult
    ) -> Records:
        refs = step.inputs
        raw = outputs.raw if isinstance(outputs, Outputs) else outputs.__getitem__
        data: Any = None
        if step.id in self.replay:
            data = self.replay[step.id]
        elif len(refs) == 1:
            data = raw(refs[0])
        elif refs:
            data = {r: outputs[r] for r in refs}

        if step.kind == "wait":
            return self._run_wait(run_id, step, outputs, [] if data is None else data, result)

        if step.kind == "check":
            if isinstance(data, dict):
                raise ValueError("check steps take a single input")
            return self._run_check(run_id, step, [] if data is None else data, result)

        if step.kind == "agent":
            if isinstance(data, dict):
                raise ValueError("agent steps take a single input; join the inputs first")
            # A source agent step (no input) runs once; an empty upstream means nothing to do.
            return self._run_agent(run_id, step, [{}] if data is None else data, result)

        if step.use == "trigger":
            return [dict(r) for r in self.trigger]

        if step.use == "sql":
            # Each input is a table named after its step; Arrow outputs are passed as they are.
            from .connectors import duck

            if step.id in self.replay:
                tables = data if isinstance(data, dict) else {(refs or ["input"])[0]: data}
            else:
                tables = {r: raw(r) for r in refs}
            return duck.run(step.id, tables, base_dir=self.spec.base_dir, **step.with_)
        if step.use == "resolve":
            return self._run_resolve(run_id, step, refs, outputs, data, result)
        if step.id in self._arrow_out or _is_table(data):
            return self._run_arrow(step, data)
        fn = registry.get(step.use) if step.use else registry.resolve_fn(step.fn or "", self.spec.base_dir)
        key = _hash("step", step.model_dump(by_alias=True), data)
        if self.use_cache and step.cached and (hit := self.store.cache_get(key)) is not None:
            return hit
        token = registry._AFTER_RUN.set(self._after_run)
        state = registry._STEP_STATE.set(self._step_state(step.id))
        try:
            out = fn(data, **step.with_)
        finally:
            registry._STEP_STATE.reset(state)
            registry._AFTER_RUN.reset(token)
        if isinstance(out, registry.Rejects) and out.rejected:
            self._rejects(run_id, step.id, out.rejected, len(out) + len(out.rejected), result)
        out = list(out) if out is not None else []
        if self.use_cache and step.cached:
            self.store.cache_set(key, out)
        return out

    def _run_resolve(
        self, run_id: str, step: StepSpec, refs: list[str], outputs: dict, data: Any, result: RunResult
    ) -> Records:
        """Exact and fuzzy matches cost nothing; only ambiguous values go to the model, once each:
        its decisions are stored per pipeline and step (`agentl resolve export|import`)."""
        from . import resolve as res

        opts = dict(step.with_)
        field, reference = opts.pop("field"), opts.pop("reference")
        use_model = opts.pop("model", True)
        instruction = opts.pop("instruction", None) or res.INSTRUCTION
        inputs = data if isinstance(data, dict) else {(refs or ["input"])[0]: list(data or [])}
        ref_id = reference if isinstance(reference, str) else (reference or {}).get("step")
        rows = [r for k, v in inputs.items() if k != ref_id for r in v]
        table = res.reference_rows(reference, inputs, self.spec.base_dir)
        memory = res.decisions_key(self.spec.name, step.id)
        decisions = dict(self.store.cache_get(memory) or {}) if self.use_cache else {}
        before = dict(decisions)

        def ask(record: dict) -> tuple[str | None, Any]:
            self._check_budget(result)
            prov, spec = self._provider(step.llm), self.spec.llm[step.llm]
            try:
                out = prov.structured(instruction, record, res.Choice)
            except llm.InvalidOutput as e:
                self._charge(result, step, spec, e)
                return None, e
            self._charge(result, step, spec, out)
            return getattr(out.value, "match", None), out

        out, counts, reused = res.resolve(
            rows, table, field, ask=ask if use_model else None, decisions=decisions, **opts
        )
        if self.use_cache and decisions != before:
            self.store.cache_set(memory, decisions)
        self._emit(run_id, "resolve.counts", step.id, **counts, cached=reused)
        if out.rejected:
            self._rejects(run_id, step.id, out.rejected, len(out) + len(out.rejected), result)
        return list(out)

    # checks ---------------------------------------------------------------
    def _run_check(self, run_id: str, step: StepSpec, records: Records, result: RunResult) -> Records:
        """Fixed expectations, then the optional agent review of the data's profile. Emits
        `check.passed` or `check.failed`; with `on_fail: stop` a failure stops the step before the
        data reaches the next one (usually the writer). The profile of each passing run is kept to
        compare the next run against."""
        records = list(records)
        memory = _hash("check-profile", self.spec.name, step.id)
        previous = self.store.cache_get(memory)
        with self._spend:
            stats = {k: (v[0], v[1]) for k, v in self._reject_stats.items()}
        findings = checks.run_expectations(
            records, step.expect, previous, rejected=stats, step=step.check or ""
        )
        prof = checks.profile(records)
        notes: list[str] = []
        if step.review:
            llm_spec = self.spec.llm[step.llm]
            schema = llm.schema_model(f"{step.id}_review", {"passed": "bool", "findings": "list[str]"})
            payload = {
                "today": prof,
                "last_delivered_run": previous,
                "failed_fixed_checks": list(findings),
            }
            self._check_budget(result)
            try:
                res = self._provider(step.llm).structured(_review_instruction(step.review), payload, schema)
            except (ValidationError, ValueError, RuntimeError) as e:
                self._charge(result, step, llm_spec, e)
                raise RuntimeError(f"check review did not answer in the expected shape: {e}") from e
            self._charge(result, step, llm_spec, res)
            verdict = res.value.model_dump()
            if verdict["passed"]:
                notes = list(verdict["findings"])
            else:
                findings += [{"check": "review", "detail": f} for f in verdict["findings"]] or [
                    {"check": "review", "detail": "the reviewer rejected the data without a reason"}
                ]
        data = {"rows": len(records), "findings": findings, **({"notes": notes} if notes else {})}
        if findings:
            self._emit(run_id, "check.failed", step.id, on_fail=step.on_fail, **data)
            if step.on_fail == "stop":
                first = "; ".join(f["detail"] for f in findings[:3])
                more = f" (+{len(findings) - 3} more)" if len(findings) > 3 else ""
                raise CheckFailed(f"{len(findings)} problem(s): {first}{more}")
        else:
            self._emit(run_id, "check.passed", step.id, **data)
            self.store.cache_set(memory, {k: v for k, v in prof.items() if k != "sample"})
        return records

    # waits ------------------------------------------------------------------
    def _run_wait(
        self, run_id: str, step: StepSpec, outputs: dict[str, Records], records: Any, result: RunResult
    ) -> Records:
        """Re-run the source step every `every` until the condition holds or `timeout` passes.
        The first poll is the source's normal output. Emits `wait.polled` for each poll, then
        `wait.done` or `wait.timeout`."""
        w = step.wait_until
        assert w is not None
        source = next(s for s in self.spec.steps if s.id == w.step).model_copy(update={"cache": False})
        every, timeout = checks.parse_duration(w.every), checks.parse_duration(w.timeout)
        memory = _hash("wait-last", self.spec.name, step.id)
        last = self.store.cache_get(memory)
        started = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            rows = records.to_pylist() if _is_table(records) else list(records)
            ok = waits.holds(w.condition, rows, last)
            self._emit(run_id, "wait.polled", step.id, attempt=attempt, rows=len(rows), satisfied=ok)
            waited = round(time.monotonic() - started, 3)
            if ok:
                self._emit(run_id, "wait.done", step.id, attempts=attempt, seconds=waited, rows=len(rows))
                self.store.cache_set(memory, waits.fingerprint(rows))
                return rows
            if waited + every > timeout:
                self._emit(
                    run_id,
                    "wait.timeout",
                    step.id,
                    attempts=attempt,
                    seconds=waited,
                    condition=w.condition,
                    on_timeout=w.on_timeout,
                )
                if w.on_timeout == "fail":
                    raise WaitTimeout(
                        f"'{w.condition}' did not hold for step '{w.step}' after {attempt} poll(s) "
                        f"in {w.timeout}"
                    )
                return rows
            time.sleep(every)
            records = self._run_step(run_id, source, outputs, result)

    # arrow fast path ------------------------------------------------------
    def _arrow_plan(self, chains: dict[str, list[StepSpec]]) -> set[str]:
        """Steps whose output stays a pyarrow Table: the step can produce one (an Arrow reader or a
        `@dataframe` fn) and every consumer can take one (an Arrow writer that accepts its settings,
        or a `@dataframe` fn with that single input). Cached and chunked steps keep records."""
        wanted = self.spec.arrow
        if wanted is False:
            return set()
        if not registry.arrow_available():
            if wanted:
                raise ImportError("arrow: true needs pyarrow: pip install 'agentl-etl[arrow]'")
            return set()
        chunked = {s.id for chain in chains.values() for s in chain}

        def is_frame(step: StepSpec) -> bool:
            if not step.fn:
                return False
            return bool(getattr(registry.resolve_fn(step.fn, self.spec.base_dir), "agentl_dataframe", False))

        def takes(step: StepSpec, src: str) -> bool:
            if step.use == "sql":  # DuckDB scans a Table in place, with any number of inputs
                return not step.cached and step.id not in chunked
            if step.inputs != [src] or step.cached or step.id in chunked:
                return False
            if step.use:
                writer = registry.arrow(step.use)[1]
                return writer is not None and writer[1](step.with_)
            return is_frame(step)

        consumers: dict[str, list[StepSpec]] = {s.id: [] for s in self.spec.steps}
        for s in self.spec.steps:
            for ref in s.inputs:
                consumers[ref].append(s)

        def polars_frame(step: StepSpec) -> bool:
            fn = registry.resolve_fn(step.fn, self.spec.base_dir) if step.fn else None
            return getattr(fn, "agentl_engine", None) == "polars"

        out = set()
        self._polars_fed = set()
        for step in self.spec.steps:
            if step.cached or step.id in chunked or not consumers[step.id] or step.schema_ is not None:
                continue  # a contract checks records, so a step with `schema:` outputs records
            makes = (step.use and registry.arrow(step.use)[0] is not None) or is_frame(step)
            if makes and all(takes(c, step.id) for c in consumers[step.id]):
                out.add(step.id)
                if step.use and all(polars_frame(c) for c in consumers[step.id]):
                    self._polars_fed.add(step.id)  # its reader may let polars read the file
        return out

    def _run_arrow(self, step: StepSpec, data: Any) -> Any:
        """Runs a step on the Arrow path. Its output is a Table when it is in the plan; a reader may
        still return records when the file needs the normal reader."""
        produce = step.id in self._arrow_out
        if step.use:
            reader, writer = registry.arrow(step.use)
            if produce and reader is not None and not _is_table(data):
                if step.id in self._polars_fed and "_engine" in inspect.signature(reader).parameters:
                    return reader(data, _engine="polars", **step.with_)
                return reader(data, **step.with_)
            if _is_table(data) and writer is not None:
                return list(writer[0](data, **step.with_) or [])
            return list(
                registry.get(step.use)(data.to_pylist() if _is_table(data) else data, **step.with_) or []
            )
        fn = registry.resolve_fn(step.fn or "", self.spec.base_dir)
        out = fn(data, _arrow=produce, **step.with_)
        return out if produce else list(out or [])

    # run ----------------------------------------------------------------
    def _run_one(self, run_id: str, step: StepSpec, result: RunResult) -> None:
        step_id = step.id
        if step.kind == "agent" or step.review or step.use == "resolve":
            with self._spend:
                result.spend[step_id] = dict.fromkeys(
                    ("input_tokens", "output_tokens", "cache_read_tokens", "llm_calls", "tool_calls"), 0
                ) | {"cost_usd": 0.0}
        self._emit(run_id, "step.started", step_id, kind=step.kind)
        t0 = time.time()
        try:
            if step.cursor:
                step = self._with_cursor(step)
            out = self._run_step(run_id, step, result.outputs, result)
            if step.schema_ is not None:
                out = self._contract(run_id, step, out, result)
            self._write_rejected(step, result)
            if step.cursor:
                self._track_cursor(step, out)
        except Exception as e:
            if isinstance(e, BudgetExceeded):
                with self._spend:
                    totals = (result.input_tokens, result.output_tokens, round(result.cost_usd, 6))
                self._emit(
                    run_id,
                    "budget.exceeded",
                    step_id,
                    reason=str(e),
                    input_tokens=totals[0],
                    output_tokens=totals[1],
                    cost_usd=totals[2],
                )
            if getattr(e, "snapshot", None):  # html.select, html.read, crawl: what the site sent
                self._emit(
                    run_id,
                    "selector.missed",
                    step_id,
                    selector=getattr(e, "selector", None),
                    url=getattr(e, "url", None),
                    snapshot=e.snapshot,  # type: ignore[attr-defined]
                )
            self._emit(
                run_id,
                "step.failed",
                step_id,
                error=_describe(e),
                **self._step_spend(step, result),
            )
            raise
        result.outputs[step_id] = out
        self._sample(run_id, step_id, out)
        # writers return one summary record per file; say how many records they wrote
        written = sum(r.get("written", 0) for r in out) if isinstance(out, list) else 0
        # upserts also say how many rows were inserted, updated or left unchanged
        counts: dict[str, int] = {}
        for r in out if isinstance(out, list) else []:
            for k in UPSERT_COUNTS:
                if isinstance(r, dict) and isinstance(r.get(k), int):
                    counts[k] = counts.get(k, 0) + r[k]
        if counts:
            result.writes[step_id] = counts
        self._emit(
            run_id,
            "step.succeeded",
            step_id,
            records=len(out),
            **({"written": written} if written else {}),
            **counts,
            seconds=round(time.time() - t0, 3),
            **self._step_spend(step, result),
        )
        if len(out) == 0 and not step.inputs:
            self._emit(run_id, "source.empty", step_id)

    def _sample(self, run_id: str, step_id: str, out: Any) -> None:
        """Keep the first records of each step in the store, so an agent can look at what a run
        produced (`show_output`, #164) without opening files. Values are cut at 200 characters;
        AGENTL_SAMPLES=0 turns it off."""
        if os.environ.get("AGENTL_SAMPLES", "1").lower() in ("0", "false", "no"):
            return
        step = next((s for s in self.spec.steps if s.id == step_id), None)
        if step is not None and step.use == "unprotect":  # decrypted values never reach the store
            return
        try:
            from . import secrets

            head = out.slice(0, SAMPLE_ROWS).to_pylist() if _is_table(out) else list(out[:SAMPLE_ROWS])
            hidden = secrets.sensitive()  # a secret that ended up in the data never reaches the store
            rows = [_clip_record(_mask_values(r, hidden) if hidden else r) for r in head]
            self.store.sample_set(run_id, step_id, rows, len(out))
        except Exception:  # a sample is a convenience; it never fails a run
            pass

    def _quarantine(self, run_id: str, step_id: str, bad: Records) -> None:
        """Keep rejected records in the store, whole and with their reason, to list and retry them
        (#180). Secret values are masked as in samples; AGENTL_QUARANTINE=0 turns it off."""
        if os.environ.get("AGENTL_QUARANTINE", "1").lower() in ("0", "false", "no"):
            return
        try:
            from . import secrets

            hidden = secrets.sensitive()
            rows = [_mask_values(r, hidden) for r in bad] if hidden else list(bad)
            self.store.rejected_add(run_id, self.spec.name, step_id, rows)
        except Exception:  # like samples, the quarantine never fails a run
            pass

    # chunked execution ----------------------------------------------------
    def _chains(self) -> dict[str, list[StepSpec]]:
        """With `chunk_size`, the linear chains the runner can stream: a source with a stream
        reader, then steps that each have that one input and are its only consumer (row-wise
        connectors and agent steps), optionally ending in a stream writer. Keyed by the source id.
        Anything else (dedupe, join, fn steps, fan-out) is a boundary where records are collected."""
        if not self.spec.chunk_size:
            return {}
        consumers: dict[str, list[StepSpec]] = {s.id: [] for s in self.spec.steps}
        for s in self.spec.steps:
            for ref in s.inputs:
                consumers[ref].append(s)
        chains = {}
        for head in self.spec.steps:
            if not head.use or head.inputs or head.cursor or registry.streaming(head.use)[0] is None:
                continue
            chain, cur = [head], head
            while len(consumers[cur.id]) == 1:
                nxt = consumers[cur.id][0]
                if nxt.inputs != [cur.id]:
                    break
                if nxt.kind == "agent":
                    if nxt.mode == "batch":
                        break  # one provider batch per chunk would defeat the point
                elif nxt.use:
                    _, writer, row = registry.streaming(nxt.use)
                    if writer is not None:
                        chain.append(nxt)
                        break
                    if not row:
                        break
                else:
                    break
                chain.append(nxt)
                cur = nxt
            if len(chain) > 1:
                chains[head.id] = chain
        return chains

    def _run_chain(self, run_id: str, chain: list[StepSpec], result: RunResult) -> None:
        """Runs a chain from `_chains` chunk by chunk: one chunk per step in memory at a time, and
        only the last step's output is kept."""
        size = self.spec.chunk_size or 1
        head, last = chain[0], chain[-1]
        for step in chain:
            if step.kind == "agent":
                with self._spend:
                    result.spend[step.id] = dict.fromkeys(
                        ("input_tokens", "output_tokens", "cache_read_tokens", "llm_calls", "tool_calls"), 0
                    ) | {"cost_usd": 0.0}
            self._emit(run_id, "step.started", step.id, kind=step.kind, chunk_size=size)
        counts = dict.fromkeys((s.id for s in chain), 0)
        failing: list[str] = []
        t0 = time.time()

        def source() -> Iterator[Records]:
            reader = registry.streaming(head.use or "")[0]
            assert reader is not None
            try:
                for chunk in reader(None, chunk_size=size, **head.with_):
                    seen = counts[head.id]
                    counts[head.id] += len(chunk)
                    if head.schema_ is not None:
                        chunk = self._contract(run_id, head, chunk, result, start=seen)
                    yield chunk
            except Exception:
                failing.append(head.id)
                raise

        def stage(chunks: Iterator[Records], step: StepSpec) -> Iterator[Records]:
            fn = None if step.kind == "agent" else registry.get(step.use or "")
            seen = 0
            try:
                for chunk in chunks:
                    if fn is None:
                        out = self._run_agent(run_id, step, chunk, result)
                    else:
                        out = fn(chunk, **step.with_)
                        if isinstance(out, registry.Rejects) and out.rejected:
                            self._rejects(run_id, step.id, out.rejected, len(out) + len(out.rejected), result)
                        out = list(out or [])
                    before = len(out)
                    if step.schema_ is not None:
                        out = self._contract(run_id, step, out, result, start=seen)
                    seen += before
                    counts[step.id] += len(out)
                    yield out
            except Exception:
                failing.append(step.id)  # an upstream failure is already first in the list
                raise

        chunks = source()
        for step in chain[1:-1]:
            chunks = stage(chunks, step)
        writer = registry.streaming(last.use)[1] if last.use else None
        try:
            if writer is not None:
                try:
                    out = list(writer(chunks, **last.with_) or [])
                except Exception:
                    failing.append(last.id)
                    raise
                counts[last.id] = len(out)
                if last.schema_ is not None:
                    out = self._contract(run_id, last, out, result)
            else:
                out = [r for chunk in stage(chunks, last) for r in chunk]
            for step in chain:
                self._write_rejected(step, result)
        except Exception as e:
            culprit = failing[0] if failing else last.id
            if isinstance(e, BudgetExceeded):
                with self._spend:
                    totals = (result.input_tokens, result.output_tokens, round(result.cost_usd, 6))
                self._emit(
                    run_id,
                    "budget.exceeded",
                    culprit,
                    reason=str(e),
                    input_tokens=totals[0],
                    output_tokens=totals[1],
                    cost_usd=totals[2],
                )
            for step in chain:
                error = _describe(e) if step.id == culprit else f"stopped: step '{culprit}' failed"
                self._emit(run_id, "step.failed", step.id, error=error, **self._step_spend(step, result))
            raise
        result.outputs[last.id] = out
        seconds = round(time.time() - t0, 3)
        for step in chain:
            self._emit(
                run_id,
                "step.succeeded",
                step.id,
                records=counts[step.id],
                seconds=seconds,
                chunked=True,
                **self._step_spend(step, result),
            )
        if counts[head.id] == 0:
            self._emit(run_id, "source.empty", head.id)

    def _run_graph(self, run_id: str, result: RunResult) -> None:
        """Starts each step as soon as its inputs are done, up to `max_parallel_steps` at once,
        picking ready steps in declaration order. After a failure no new step starts; the ones
        already running finish, then the first error is raised."""
        chains = self._chains()
        self._arrow_out = self._arrow_plan(chains)
        inside = {s.id for chain in chains.values() for s in chain[1:]}
        pending = [i for i in graph.order(self.spec) if i not in inside]
        deps = {s.id: set(s.inputs) for s in self.spec.steps}
        limit = self.spec.max_parallel_steps
        done: set[str] = set()
        running: dict[Future, str] = {}
        failure: BaseException | None = None
        with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="agentl-step") as pool:
            while pending or running:
                if failure is None:
                    for step_id in [i for i in pending if deps[i] <= done][: limit - len(running)]:
                        pending.remove(step_id)
                        if step_id in chains:
                            future = pool.submit(self._run_chain, run_id, chains[step_id], result)
                        else:
                            future = pool.submit(self._run_one, run_id, self.spec.step(step_id), result)
                        running[future] = step_id
                if not running:
                    break
                finished, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in sorted(finished, key=lambda f: running[f]):
                    step_id = running.pop(future)
                    error = future.exception()
                    if error is None:
                        done.update(s.id for s in chains.get(step_id, [self.spec.step(step_id)]))
                    elif failure is None:
                        failure = error
        if failure is not None:
            raise failure

    def run(self) -> RunResult:
        run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        result = RunResult(run_id=run_id, status="running")
        self._run_id = run_id
        self.store.start_run(run_id, self.spec.name, graph.mermaid(self.spec))
        self._emit(run_id, "run.started", steps=len(self.spec.steps))
        started = time.time()
        try:
            self._run_graph(run_id, result)
            result.status = "succeeded"
        except Exception as e:
            result.status = "failed"
            result.error = _describe(e)
        finally:
            if self._toolbox is not None:  # MCP servers started for this run
                self._toolbox.close()
                self._toolbox = None
        self._finish_after_run(run_id, result)
        self._save_cursors(run_id, result)
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

    # incremental loading -------------------------------------------------
    def _with_cursor(self, step: StepSpec) -> StepSpec:
        """The step with `{cursor}` in its `with` replaced by the stored cursor (or `initial`)."""
        assert step.cursor is not None
        stored = self.store.state_get(self.spec.name, step.id)
        value = step.cursor.initial if stored is None else stored

        def sub(v: Any) -> Any:
            if isinstance(v, str):
                if v == "{cursor}":
                    return value  # keeps the type: a number stays a number
                return v.replace("{cursor}", "" if value is None else str(value))
            if isinstance(v, dict):
                return {k: sub(x) for k, x in v.items()}
            if isinstance(v, list):
                return [sub(x) for x in v]
            return v

        self._cursor_start[step.id] = value
        return step.model_copy(update={"with_": sub(step.with_)})

    def _track_cursor(self, step: StepSpec, out: Any) -> None:
        assert step.cursor is not None
        field = step.cursor.field
        values = out.column(field).to_pylist() if _is_table(out) else [r.get(field) for r in out]
        best = self._cursor_start.get(step.id)
        for v in values:
            if v is None:
                continue
            try:
                if best is None or v > best:
                    best = v
            except TypeError as e:  # e.g. "2026-01-01" vs 5
                raise ValueError(
                    f"step '{step.id}': cursor field '{field}' mixes types ({best!r} and {v!r})"
                ) from e
        self._cursor_new[step.id] = best

    def _save_cursors(self, run_id: str, result: RunResult) -> None:
        """Cursors move only when the whole run succeeded, so a failed run reads the same data again."""
        new, self._cursor_new = self._cursor_new, {}
        start, self._cursor_start = self._cursor_start, {}
        if result.status != "succeeded":
            return
        for step_id, value in new.items():
            if value is not None and value != start.get(step_id):
                self.store.state_set(self.spec.name, step_id, value, run_id)
                self._emit(run_id, "cursor.advanced", step_id, previous=start.get(step_id), cursor=value)

    def _step_state(self, step_id: str) -> registry.StepState:
        """Checkpoints of stream readers, stored like cursors (see `registry.step_state`)."""
        run_id = self._run_id or ""

        def put(value: Any) -> None:
            self.store.state_set(self.spec.name, step_id, value, run_id)
            self._emit(run_id, "cursor.advanced", step_id, cursor=value)

        return registry.StepState(lambda: self.store.state_get(self.spec.name, step_id), put)

    def _finish_after_run(self, run_id: str, result: RunResult) -> None:
        """Acknowledge (on success) or release (on failure) what queue consumers read. A failed
        acknowledgement fails the run: the messages will be delivered again."""
        hooks, self._after_run = self._after_run, []
        for on_success, on_failure in hooks:
            try:
                if result.status == "succeeded":
                    on_success()
                elif on_failure is not None:
                    on_failure()
            except Exception as e:
                self._emit(run_id, "ack.failed", error=_describe(e))
                if result.status == "succeeded":
                    result.status = "failed"
                    result.error = f"acknowledging the messages read failed: {type(e).__name__}: {e}"


def _review_instruction(review: str) -> str:
    return (
        f"{review}\n\nYou are checking data before it is delivered to the people who use it. The "
        "record holds a profile of today's data (row count, per-column stats, a few sample rows), "
        "the profile of the last delivered run (null on the first run) and the fixed checks that "
        "already failed. Set passed to false only for problems that should block delivery. Write "
        "each finding as one short sentence a business user understands, with the numbers."
    )


def _batch_schema(step_id: str, schema: type[BaseModel]) -> type[BaseModel]:
    """The provider is asked for items in the step's schema (plus `index`), but the batch accepts
    any objects: each item is validated on its own, so one bad item does not void the batch."""
    item = create_model(f"{step_id}_item", __base__=schema, index=(int, ...)).model_json_schema()
    item.pop("title", None)
    items = Field(..., json_schema_extra={"items": item})
    return create_model(f"{step_id}_batch", items=(list[dict[str, Any]], items))


def _batch_instruction(instruction: str, n: int) -> str:
    return (
        f'{instruction}\n\nThe record holds a list of {n} records under "records", each with an '
        '"index". Apply the instruction to each record on its own and answer with exactly one '
        f'item per record in "items", with the same index ({n} items).'
    )


def _downstream(spec: PipelineSpec, step_id: str) -> list[StepSpec]:
    """The steps a retry of `step_id` runs: the step, every step downstream of it, and whatever
    those also read from outside that set (other branches of a join run again, from their cache)."""
    below = {step_id}
    changed = True
    while changed:
        changed = False
        for s in spec.steps:
            if s.id not in below and below & set(s.inputs):
                below.add(s.id)
                changed = True
    keep = set(below)
    todo = [ref for s in spec.steps if s.id in below and s.id != step_id for ref in s.inputs]
    while todo:
        ref = todo.pop()
        if ref not in keep:
            keep.add(ref)
            todo.extend(spec.step(ref).inputs)
    return [s for s in spec.steps if s.id in keep]


def retry_rejected(
    spec: PipelineSpec,
    store: Store,
    step: str | None = None,
    ids: list[int] | None = None,
    **kwargs: Any,
) -> RunResult | None:
    """Run the pending rejected records of one step again, through that step and the steps
    downstream of it, in a new run. Records that passed are not run again, and agent steps keep
    their per-record cache. Retried records are marked with the new run id when it succeeds;
    records rejected again go back to the quarantine under the new run. None: nothing to retry."""
    rows = store.rejected_list(spec.name, step, pending=True)
    if ids is not None:
        rows = [r for r in rows if r["id"] in set(ids)]
    if not rows:
        return None
    steps = sorted({r["step"] for r in rows})
    if len(steps) > 1:
        raise ValueError(f"rejected records of several steps ({', '.join(steps)}): pick one with --step")
    step_id = steps[0]
    if step_id not in {s.id for s in spec.steps}:
        raise ValueError(f"step '{step_id}' is not in pipeline '{spec.name}'")
    hidden = {"_error", "_raw"}
    records = [{k: v for k, v in r["record"].items() if k not in hidden} for r in rows]
    picked = [
        s.model_copy(update={"input": None}) if s.id == step_id else s for s in _downstream(spec, step_id)
    ]
    sub = spec.model_copy(update={"steps": picked, "chunk_size": None, "arrow": False, "trigger": []})
    result = Runner(sub, store=store, replay={step_id: records}, **kwargs).run()
    if result.status == "succeeded":
        store.rejected_retried([r["id"] for r in rows], result.run_id)
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
