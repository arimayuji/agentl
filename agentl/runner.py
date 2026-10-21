"""Runs a pipeline: executes steps in graph order, caches agent calls per
record, emits events to the store, the dashboard and notifications."""

from __future__ import annotations

import contextlib
import contextvars
import datetime as _dt
import hashlib
import inspect
import itertools
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, create_model

from . import checks, contracts, drift, graph, llm, registry, waits
from . import labels as run_labels
from .notify import Notifier
from .spec import DROP, LLM_KEY_EXCLUDE, LLMSpec, PiiSpec, PipelineSpec, RetrySpec, ReviewSpec, StepSpec, load
from .store import Store, open_store
from .tools import ToolBox, api_name

log = logging.getLogger("agentl.runner")

Records = list[dict[str, Any]]
Listener = Callable[[dict], None]


@dataclass
class RunResult:
    run_id: str
    status: str  # running, succeeded, failed, or skipped (only with outcomes.on_transport_error)
    outputs: dict[str, Records] = field(default_factory=lambda: Outputs())
    rejected: dict[str, Records] = field(default_factory=dict)
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0  # part of input_tokens served from provider prompt caches
    llm_calls: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    throttled_s: float = 0.0  # seconds llm calls waited for a client-side rate_limit
    spend: dict[str, dict[str, Any]] = field(default_factory=dict)  # per agent step
    # per upsert, changeset or scd2 step: {"inserted": n, "updated": n, "deleted": n, "unchanged": n}
    # (scd2 also: versioned, closed)
    writes: dict[str, dict[str, int]] = field(default_factory=dict)
    # success, no_change, skipped, partial or failed (#188); see `outcome_status` for the mapping
    outcome: str | None = None
    reason: str | None = None  # why the run was skipped (or failed after too many skips)
    # best-effort steps (`required: false`) that failed: {step_id: error}. The run still succeeds.
    best_effort_failed: dict[str, str] = field(default_factory=dict)
    # steps whose output came from a fallback body (#296): {step_id: "fallback[i]"}
    fallbacks: dict[str, str] = field(default_factory=dict)
    # isolated steps (`isolate: per_series`, #206) with series that failed: {step_id: {series: error}}
    series_failed: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def partial(self) -> bool:
        """Succeeded, but at least one best-effort step (or isolated series) failed."""
        return self.status == "succeeded" and bool(self.best_effort_failed or self.series_failed)

    def summary(self, outputs: bool = False) -> dict[str, Any]:
        """JSON-serializable view for task results and logs. Record data only with `outputs=True`."""
        data = asdict(self)
        data["records"] = {k: len(v) for k, v in self.outputs.items()}
        data["rejected"] = {k: len(v) for k, v in self.rejected.items()}
        data["partial"] = self.partial
        if not outputs:
            data.pop("outputs")
        return json.loads(json.dumps(data, default=str))


SAMPLE_ROWS = 5
UPSERT_COUNTS = ("inserted", "updated", "deleted", "unchanged", "versioned", "closed")
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


def _write_counts(out: Any) -> tuple[int, dict[str, int]]:
    """Rows a writer wrote (the sum of `written` over its summary records, one per file or table)
    and, for upserts, how many were inserted, updated or left unchanged."""
    written = 0
    counts: dict[str, int] = {}
    if isinstance(out, registry.Changes):  # emit: changes; the summary has the counts
        out = out.summary
    for r in out if isinstance(out, list) else []:
        if not isinstance(r, dict):
            continue
        if isinstance(r.get("written"), int):
            written += r["written"]
        for k in UPSERT_COUNTS:
            if isinstance(r.get(k), int):
                counts[k] = counts.get(k, 0) + r[k]
    return written, counts


def _seen(out: Any) -> int:
    """Records a step that returned `Rejects` looked at: a writer's rows written plus the ones it
    rejected; any other step's output plus its rejects."""
    rows = out.summary if isinstance(out, registry.Changes) else list(out)
    is_writer = any(isinstance(r, dict) and "written" in r for r in rows)
    return (_write_counts(rows)[0] if is_writer else len(rows)) + len(out.rejected)


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


def _today() -> str:
    """The digest's day bucket (UTC)."""
    return _dt.datetime.now(_dt.timezone.utc).date().isoformat()


def _digest_text(diff: dict[str, list[str]], cap: int) -> str:
    lines = []
    for name in ("new", "resolved", "ongoing"):
        items = diff[name]
        lines.append(f"{name}: {len(items)}")
        lines += [f"  - {i}" for i in items[:cap]]
    return "\n".join(lines)


class CheckFailed(RuntimeError):
    """A check step with `on_fail: stop` found problems; the data was not passed on."""


class SchemaDrift(CheckFailed):
    """A check step with `on_drift: fail` found that its input changed layout."""


class WaitTimeout(RuntimeError):
    """A `wait_until` step with `on_timeout: fail` ran out of time before its condition held."""


class BudgetExceeded(RuntimeError):
    """The run reached `budget.max_tokens` or `budget.max_usd`."""


def _page_chunks(step: StepSpec, records: Records) -> Iterator[tuple[int, dict, dict]]:
    """Rows mode with `chunk: {by: page}` (#215): (source index, record, page info) per call.
    Without `chunk`, each record goes whole. A record without the field, or with one page or
    less per group, also goes whole. Each group replaces the field with its pages joined by form
    feeds; `_pages` ([first, last], 1-based) marks the rejects of that group."""
    c = step.chunk
    for n, rec in enumerate(records):
        value = rec.get(c.field) if c else None
        if isinstance(value, str):
            pages: list = value.split("\f")
        elif isinstance(value, list):
            pages = value
        else:
            yield n, rec, {}
            continue
        assert c is not None
        if len(pages) <= c.size:
            yield n, rec, {}
            continue
        for i in range(0, len(pages), c.size):
            group = pages[i : i + c.size]
            part = "\f".join(map(str, group)) if isinstance(value, str) else group
            yield n, {**rec, c.field: part}, {"_pages": [i + 1, i + len(group)]}


def remap_targets(spec: PipelineSpec, check_id: str) -> list[str]:
    """The map_schema steps that read check `check_id` (`on_drift: remap`, #178)."""
    return [s.id for s in spec.steps if s.use == "map_schema" and check_id in s.inputs]


def agent_cache_key(spec: PipelineSpec, step: StepSpec, rec: dict, prompts: bool = True) -> str:
    """The per-record cache key of an agent step (also used by `agentl plan` to count hits).
    `prompts` adds the built-in prompt version (#338); the review queue leaves it out, so a
    person's answer survives an agentl upgrade that only rewords those prompts."""
    # Without a cascade the key stays what it was, so existing caches keep their hits.
    llm_key: Any = spec.llm[step.llm].model_dump(exclude=LLM_KEY_EXCLUDE)
    if step.attempts:
        llm_key = [
            [spec.llm[a.llm or step.llm].model_dump(exclude=LLM_KEY_EXCLUDE), a.validate_]
            for a in step.attempts
        ]
    elif step.escalate:
        llm_key = [spec.llm[n].model_dump(exclude=LLM_KEY_EXCLUDE) for n in [step.llm, *step.escalate]]
    if step.jury is not None:  # the cached answer is the jury's decision
        llm_key = [llm_key, step.jury.model_dump(), step.jury_tag]
    salt = _key_salt(step, prompts)
    # records_per_call is not part of the key: the answer per record is the same.
    if step.routes is not None:  # a router's answer depends on its labels, not on where they go
        return _hash("route", step.agent, sorted(step.routes), llm_key, step.tools, rec, *salt)
    if step.output_mode == "rows":  # the cached value is a list of rows, not one answer
        return _hash("agent-rows", step.agent, step.output, llm_key, rec, *salt)
    if step.tools:  # tools can change the answer, so they are part of the key
        return _hash("agent", step.agent, step.output, llm_key, step.tools, rec, *salt)
    return _hash("agent", step.agent, step.output, llm_key, rec, *salt)


def _key_salt(step: StepSpec, prompts: bool = True) -> list:
    """What every cached llm answer of a step also depends on: the built-in prompt version and the
    step's own `cache_version`."""
    salt: list = [llm.PROMPT_VERSION] if prompts else []
    if step.cache_version is not None:
        salt.append({"cache_version": step.cache_version})
    return salt


def _review_reason(review: ReviewSpec, answer: dict) -> str | None:
    """Why `answer` goes to the human review queue (#153), or None when it does not. A missing
    confidence counts as low, and a `when` that fails to evaluate counts as matching."""
    if review.always:
        return "always"
    if review.below is not None:
        conf = answer.get("confidence")
        if not isinstance(conf, int | float) or isinstance(conf, bool):
            return f"no confidence (below {review.below})"
        if conf < review.below:
            return f"confidence {conf} below {review.below}"
    if review.when is not None:
        from . import expr

        try:
            hit = expr.evaluate(review.when, answer)
        except Exception as e:
            return f"when {review.when!r} failed: {e}"
        if hit:
            return f"when {review.when}"
    return None


def _trace(e: BaseException) -> str:
    """The end of the traceback, kept on `step.failed` so `agentl report` can show it."""
    import traceback

    text = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    return text[-TRACE_CHARS:]


TRACE_CHARS = 3000


def _describe(e: BaseException) -> str:
    text = f"{type(e).__name__}: {e}"
    if "CERTIFICATE_VERIFY_FAILED" in text:  # netguard is not imported up front: it is heavy
        from .netguard import describe

        return describe(e)
    return text


_TRANSPORT_NAMES = ("Timeout", "ConnectionError", "ConnectError", "ConnectTimeout", "ReadTimeout")


def is_transport_error(e: BaseException) -> bool:
    """A timeout or connection error anywhere in the exception chain. HTTP status errors
    (4xx/5xx) and parsing errors are not: the source answered."""
    import urllib.error

    seen: set[int] = set()
    cur: BaseException | None = e
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, urllib.error.HTTPError) or "HTTPStatus" in type(cur).__name__:
            return False
        if isinstance(cur, (TimeoutError, ConnectionError)):
            return True
        if isinstance(cur, urllib.error.URLError) and isinstance(cur.reason, (OSError, TimeoutError)):
            return True
        if any(n in t.__name__ for t in type(cur).__mro__ for n in _TRANSPORT_NAMES):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


_ALL: Any = object()  # sentinel: the step reads its whole input


def default_retries(step: StepSpec) -> RetrySpec | None:
    """Retries of a SQL writer that sets none: deadlocks and lock timeouts, plus unique-key races
    for the modes that read before they write (upsert, scd2, changeset). `retries: {attempts: 1}`
    turns them off."""
    if step.use not in SQL_WRITERS or step.retries is not None:
        return None
    on: list[Any] = ["deadlock"]
    if step.with_.get("mode", "append") in ("upsert", "scd2", "changeset"):
        on.append("unique_violation")
    return RetrySpec(attempts=3, backoff="0.2s", max_backoff="2s", on=on)


def _sleep(seconds: float) -> None:
    """Waits between step retries; tests monkeypatch it."""
    time.sleep(seconds)


def _jitter() -> float:
    """A factor in [0.5, 1) applied to each retry wait; tests monkeypatch it."""
    import random

    return 0.5 + random.random() / 2


def _http_status(e: BaseException) -> tuple[int | None, Any]:
    """(HTTP status, response headers) of the first error in the chain that carries one."""
    seen: set[int] = set()
    cur: BaseException | None = e
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        resp = getattr(cur, "response", None)
        for holder in (cur, resp):
            code = getattr(holder, "code", None) or getattr(holder, "status_code", None)
            if isinstance(code, int) and 100 <= code < 600:
                return code, getattr(holder, "headers", None)
        cur = cur.__cause__ or cur.__context__
    return None, None


# SQLSTATE codes of errors a retry of the whole transaction can clear (Postgres and others)
_DEADLOCK_STATES = {"40P01", "40001", "55P03"}  # deadlock, serialization failure, lock not available
_UNIQUE_STATES = {"23505"}
# writers whose write is one transaction, so a retry after a conflict starts clean
SQL_WRITERS = ("sqlite.write", "postgres.write", "sql.write")


def sql_conflict(e: BaseException) -> str | None:
    """`deadlock` or `unique_violation` when an error in the chain is a lock conflict or a
    unique-key race of a SQL database (psycopg, sqlite3 or SQLAlchemy wrapping either)."""
    seen: set[int] = set()
    cur: BaseException | None = e
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        state = getattr(cur, "sqlstate", None) or getattr(cur, "pgcode", None)
        if isinstance(state, str):
            if state in _DEADLOCK_STATES:
                return "deadlock"
            if state in _UNIQUE_STATES:
                return "unique_violation"
        name = type(cur).__name__
        text = str(cur).lower()
        if name == "OperationalError" and ("database is locked" in text or "table is locked" in text):
            return "deadlock"
        if name == "IntegrityError" and "unique constraint failed" in text:
            return "unique_violation"
        orig = getattr(cur, "orig", None)  # SQLAlchemy keeps the driver error here
        cur = orig if isinstance(orig, BaseException) else (cur.__cause__ or cur.__context__)
    return None


def _retry_matches(e: BaseException, on: list[str]) -> bool:
    if "any" in on:
        return True
    if "transport" in on and is_transport_error(e):
        return True
    if ("deadlock" in on or "unique_violation" in on) and sql_conflict(e) in on:
        return True
    status = _http_status(e)[0]
    return status is not None and (
        ("http_5xx" in on and status >= 500) or ("http_429" in on and status == 429)
    )


def _retry_after(e: BaseException) -> float | None:
    """Seconds asked by a `Retry-After` header (a number or an HTTP date), if the error has one."""
    headers = _http_status(e)[1]
    return llm.parse_retry_after(headers.get("Retry-After") if hasattr(headers, "get") else None)


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
        resume_from: str | None = None,
        raw_from: str | None = None,
    ):
        self.spec = spec
        # a failed run to resume (#339): its completed steps are skipped and their outputs read back
        self.resume_from = resume_from
        # a run whose `persist: raw` originals to reprocess with this code (#208); no source fetch
        self.raw_from = raw_from
        self._plan: Any = None
        # step id -> records that step runs on instead of its inputs (retry of rejected records)
        self.replay = dict(replay or {})
        self.trigger = list(trigger or [])  # what started this run; a `use: trigger` step reads it
        self.store = store or open_store()
        self.use_cache = use_cache
        self.listeners = list(listeners or [])
        self._providers: dict[str, llm.Provider] = {}
        self._toolbox: ToolBox | None = None
        self._spend = threading.Lock()
        # Calls that passed the budget check and have not been charged yet: ticket -> step id.
        # The check counts them at the average cost of a call, so parallel workers cannot all
        # slip past it. `_charge(ticket=...)` releases one; a step's leftovers go when it ends.
        self._inflight: dict[int, str] = {}
        self._tickets = itertools.count(1)
        self._events = threading.Lock()
        self._provider_lock = threading.Lock()
        self._limiters: dict[str, llm.RateLimiter] = {}  # llm name -> bucket shared by its calls
        self._calling = threading.local()  # step id of the call this thread is about to make
        self._result: RunResult | None = None
        self._arrow_out: set[str] = set()
        self._reject_stats: dict[str, list[int]] = {}  # step id -> [rejected, seen], for checks
        # on_drift: remap (#178): map_schema step id -> (signature key, new layout) to record
        # once that step has mapped the new layout
        self._remap: dict[str, list[tuple[str, dict]]] = {}
        self._polars_fed: set[str] = set()
        # router id -> target step id -> the records routed to it this run (#299)
        self._routed: dict[str, dict[str, Records]] = {}
        self._outputs: dict = {}
        self._run_id: str | None = None  # for events raised from inside providers (llm.fallback)
        self._cursor_start: dict[str, Any] = {}  # step id -> cursor this run started from
        self._cursor_new: dict[str, Any] = {}  # step id -> largest value seen this run
        self._source_records: dict[str, int] = {}  # records read by steps without inputs, this run
        self._after_run: list = []  # (on_success, on_failure) from connectors, see registry.after_run
        self._failed_step: str | None = None  # the step whose error failed this run (#356)
        self.listeners.append(
            Notifier(
                spec.notify,
                spec.name,
                summarize=self._summarize,
                records=self._step_records,
                store=self.store,
            )
        )
        from . import lineage

        lineage_cfg = lineage.from_env(spec.lineage)
        if lineage_cfg is not None and not any(isinstance(x, lineage.OpenLineage) for x in self.listeners):
            self.listeners.append(lineage.OpenLineage(spec, lineage_cfg))

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
        if type == "step.failed" and "traceback" in data and data.get("required", True):
            self._failed_step = self._failed_step or step  # the culprit, not the steps it stopped
        if type == "step.succeeded" and step is not None and not self.spec.step(step).inputs:
            self._source_records[step] = int(data.get("records") or 0)
        with self._events:  # steps may run in parallel: one event at a time, store and listeners
            self.store.event(run_id, type, step, data)
            for listener in self.listeners:
                listener(event)

    def _step_records(self, step_id: str) -> list | None:
        """The records a step produced in the current run (for `notify` rules `on: records`)."""
        out = self._outputs.get(step_id)
        return out.to_pylist() if out is not None and _is_table(out) else out

    # llm ----------------------------------------------------------------
    def _provider(self, name: str) -> llm.Provider:
        with self._provider_lock:
            if name not in self._providers:
                spec = self.spec.llm[name]
                prov = self._throttled(name, llm.provider(spec))
                if spec.fallback:
                    chain = [(name, spec, prov)]
                    chain += [
                        (f, self.spec.llm[f], self._throttled(f, llm.provider(self.spec.llm[f])))
                        for f in spec.fallback
                    ]
                    prov = llm.FallbackProvider(chain)
                    prov.on_fallback = self._on_fallback
                from . import cassettes  # here: it loads urllib, which `import agentl` must not

                tape = cassettes.active()
                if tape is not None:  # record or replay the answers (#211)
                    prov = cassettes.CassetteProvider(
                        tape, name, prov, lambda: getattr(self._calling, "step", None) or None
                    )
                self._providers[name] = prov
            return self._providers[name]

    def _throttled(self, name: str, prov: llm.Provider) -> llm.Provider:
        """`prov` behind the llm's rate limiter, if it has one. Called under `_provider_lock`."""
        limit = self.spec.llm[name].rate_limit
        if limit is None:
            return prov
        if name not in self._limiters:
            self._limiters[name] = llm.rate_limiter(limit)
        return llm.ThrottledProvider(prov, self._limiters[name], lambda wait: self._on_throttled(name, wait))

    def _on_throttled(self, name: str, wait: float) -> None:
        step = getattr(self._calling, "step", None) or None
        result = self._result
        if result is not None:
            with self._spend:
                result.throttled_s += wait
                mine = result.spend.get(step) if step else None
                if mine is not None:
                    mine["throttled_s"] = mine.get("throttled_s", 0.0) + wait
        if self._run_id:
            self._emit(self._run_id, "llm.throttled", step, llm=name, wait=round(wait, 3))

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
            if "throttled_s" in spend:
                spend["throttled_s"] = round(spend["throttled_s"], 3)
            out = dict(spend)
            if "models" in spend:
                out["models"] = {
                    k: {**v, "cost_usd": round(v["cost_usd"], 6)} for k, v in spend["models"].items()
                }
            return out

    # budget -------------------------------------------------------------
    def _charge(
        self,
        result: RunResult,
        step: StepSpec,
        spec: LLMSpec,
        usage: Any,
        batch: bool = False,
        ticket: int = 0,
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
            self._inflight.pop(ticket, None)
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
            # per model, so fallbacks and `agentl cost --by model` see which model was paid for
            m = mine.setdefault("models", {}).setdefault(
                spec.model, {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
            )
            m["llm_calls"] += 1
            m["input_tokens"] += tin
            m["output_tokens"] += tout
            m["cost_usd"] += cost

    def _check_budget(self, result: RunResult, step_id: str = "", reserve: bool = True) -> int:
        """Raise BudgetExceeded when the budget is spent, counting calls in flight. Returns a
        ticket for the call about to be made (0 when nothing is reserved); pass it to `_charge`."""
        self._calling.step = step_id
        with self._spend:
            reason = self._over_budget(result, len(self._inflight))
            ticket = 0
            if not reason and reserve and self.spec.budget is not None:
                ticket = next(self._tickets)
                self._inflight[ticket] = step_id
        if reason:
            raise BudgetExceeded(reason)
        return ticket

    def _release_step(self, step_id: str) -> None:
        """Reservations a step left behind (a call that raised before it was charged)."""
        with self._spend:
            for t in [t for t, s in self._inflight.items() if s == step_id]:
                del self._inflight[t]

    def _over_budget(self, result: RunResult, inflight: int = 0) -> str | None:
        b = self.spec.budget
        if b is None:
            return None
        # Calls already running are counted at the average cost of the calls charged so far.
        calls = max(1, result.llm_calls)
        tokens = result.input_tokens + result.output_tokens
        tokens += inflight * tokens // calls if result.llm_calls else 0
        usd = result.cost_usd + (inflight * result.cost_usd / calls if result.llm_calls else 0)
        if b.max_tokens is not None and tokens >= b.max_tokens:
            return f"max_tokens {b.max_tokens} reached ({result.input_tokens + result.output_tokens} used)"
        if b.max_usd is not None and usd >= b.max_usd:
            return f"max_usd {b.max_usd} reached ({result.cost_usd:.4f} spent)"
        if b.max_tool_calls is not None and result.tool_calls >= b.max_tool_calls:
            return f"max_tool_calls {b.max_tool_calls} reached"
        if b.max_calls is not None and result.llm_calls + inflight >= b.max_calls:
            return f"max_calls {b.max_calls} reached ({result.llm_calls} made)"
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
            self._check_budget(result, step.id, reserve=False)
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
        instruction: str | None = None,
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
        held = 0  # budget reservation of the submit, released by the first charge
        if saved:
            batch_id = saved["batch_id"]
            self._emit(run_id, "batch.resumed", step.id, batch_id=batch_id, requests=len(ids))
        else:
            held = self._check_budget(result, step.id)
            batch_id = prov.submit_batch(
                (step.agent or "") if instruction is None else instruction, ids, schema
            )
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
            self._charge(result, step, llm_spec, res, batch=True, ticket=held)
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
    def _run_rows(self, run_id: str, step: StepSpec, records: Records, result: RunResult) -> Records:
        """`output_mode: rows` (#215): one call per input record, answered as one JSON array per
        line. A truncated answer is continued up to `max_continuations` times; rows the model
        repeats at the seam are dropped. A row with the wrong shape is rejected on its own."""
        columns = list(step.output or {})
        schema = llm.schema_model(f"{step.id}_row", step.output or {})
        prov = self._provider(step.llm)
        if not hasattr(prov, "rows"):
            provider = self.spec.llm[step.llm].provider
            raise ValueError(f"step '{step.id}': llm provider '{provider}' does not support output_mode rows")
        spec = self.spec.llm[step.llm]
        cacheable = self.use_cache and step.cached
        stats = {"continuations": 0, "array_chars": 0, "object_chars": 0, "output_tokens": 0}

        def ask(rec: dict) -> tuple[list[list], Records]:
            got: list[list] = []
            bad: Records = []
            previous: list[str] = []
            while True:
                self._check_budget(result)
                try:
                    chunk = prov.rows(step.agent or "", rec, columns, previous)
                except (ValueError, RuntimeError) as e:
                    self._charge(result, step, spec, e)
                    raise
                self._charge(result, step, spec, chunk)
                with self._spend:
                    stats["output_tokens"] += chunk.output_tokens
                rows, lines, kept = parse_rows(chunk.text, chunk.truncated)
                rows = drop_overlap(got, rows)
                got.extend(rows)
                bad.extend({"_row": line, "_error": "row is not a JSON array"} for line in lines)
                if not chunk.truncated:
                    return got, bad
                if step.on_truncated == "fail":
                    raise ValueError("answer truncated at the output token limit (on_truncated: fail)")
                if len(previous) >= step.max_continuations:
                    raise ValueError(f"answer still truncated after {step.max_continuations} continuations")
                previous.append(kept)
                with self._spend:
                    stats["continuations"] += 1

        ok: Records = []
        bad: Records = []
        for n, rec, pages in _page_chunks(step, records):
            k = agent_cache_key(self.spec, step, rec)
            hit = self.store.cache_get(k) if cacheable else None
            raw_rows: list[list]
            raw_bad: Records
            if hit is not None:
                raw_rows, raw_bad = hit["rows"], []
            else:
                try:
                    raw_rows, raw_bad = ask(rec)
                except BudgetExceeded:
                    raise
                except (ValueError, RuntimeError) as e:
                    bad.append({**rec, **pages, "_error": str(e)})
                    continue
                if cacheable:
                    self.store.cache_set(k, {"rows": raw_rows})
            for row_bad in raw_bad:
                bad.append({**row_bad, "_source": n, **pages})
            for row in raw_rows:
                if len(row) != len(columns):
                    bad.append(
                        {
                            "_row": row,
                            "_source": n,
                            **pages,
                            "_error": f"row has {len(row)} values, expected {len(columns)}",
                        }
                    )
                    continue
                try:
                    ok.append(schema.model_validate(dict(zip(columns, row, strict=True))).model_dump())
                except ValidationError as e:
                    bad.append({"_row": row, "_source": n, **pages, "_error": str(e)})
                    continue
                if hit is None:
                    stats["array_chars"] += len(json.dumps(row, default=str))
                    stats["object_chars"] += len(json.dumps(ok[-1], default=str))
        saved = 0
        if stats["array_chars"]:
            per_char = stats["output_tokens"] / stats["array_chars"]
            saved = round((stats["object_chars"] - stats["array_chars"]) * per_char)
        with self._spend:
            mine = result.spend[step.id]
            mine["rows"] = len(ok)
            mine["continuations"] = stats["continuations"]
            mine["output_tokens_saved_est"] = max(0, saved)
        self._emit(
            run_id,
            "agent.rows",
            step.id,
            rows=len(ok),
            continuations=stats["continuations"],
            output_tokens_saved_est=max(0, saved),
        )
        self._rejects(run_id, step.id, bad, len(ok) + len(bad), result)
        return ok

    def _run_agent(self, run_id: str, step: StepSpec, records: Records, result: RunResult) -> Records:
        """An agent step. With `rules:` (#124) each record tries the rules first and only the
        records they cannot handle are sent to the llm; the output keeps the input order and each
        record says which path produced it in `_path`. `output_mode: rows` (#215) has its own path
        (one record in, many rows out), which spec validation keeps apart from `rules:`."""
        records = self._pii_gate(run_id, step, records, result)
        if step.routes is not None:
            return self._run_route(run_id, step, records, result)
        if step.output_mode == "rows":
            return self._run_rows(run_id, step, records, result)
        if not step.rules:
            answered = self._pii_masked(run_id, step, list(records), result, self._agent_reviewed)
            return self._agent_finish(run_id, step, answered, len(records), result)
        records = list(records)
        schema = llm.schema_model(f"{step.id}_output", step.output or {})
        by_rules = _apply_rules(step, records, schema, self.spec.base_dir)
        rest = [i for i, out in enumerate(by_rules) if out is None]
        asked = (
            self._pii_masked(run_id, step, [records[i] for i in rest], result, self._agent_reviewed)
            if rest
            else []
        )
        slots: list[tuple[dict | None, dict | None]] = []
        answers = iter(asked)
        for rec, out in zip(records, by_rules, strict=True):
            if out is not None:
                slots.append(({**rec, **out, "_path": "rules"}, None))
                continue
            good, bad = next(answers)
            slots.append(({**good, "_path": "agent"} if good is not None else None, bad))
        paths = {"rules": len(records) - len(rest), "agent": len(rest)}
        with self._spend:
            result.spend[step.id]["paths"] = paths
        self._emit(run_id, "hybrid.paths", step.id, **paths, total=len(records))
        return self._agent_finish(run_id, step, slots, len(records), result)

    def _run_route(self, run_id: str, step: StepSpec, records: Records, result: RunResult) -> Records:
        """A router (#299): `rules:` pick a label first; the llm labels the other records, with
        the answer constrained to the route labels (cached per record, budgeted like any agent
        call). Each target step then reads only its branch; `drop` discards and a record without
        a valid label goes to the quarantine. The step's own output is every routed record."""
        from . import expr

        routes = step.routes or {}
        labels = list(routes)
        records = list(records)
        chosen: list[str | None] = [None] * len(records)
        for i, rec in enumerate(records):
            for rule in step.rules:
                try:
                    hit = expr.evaluate(str(rule["when"]), rec)
                except Exception:  # an expression that cannot be evaluated on a record misses it
                    hit = False
                if hit is True:
                    chosen[i] = rule["route"]
                    break
        rest = [i for i, c in enumerate(chosen) if c is None]
        schema = create_model(f"{step.id}_route", route=(Literal[tuple(labels)], ...))  # type: ignore[arg-type]
        bad: Records = []
        slots = (
            self._pii_masked(
                run_id,
                step,
                [records[i] for i in rest],
                result,
                lambda r, s, recs, res: self._agent_slots(
                    r, s, recs, res, schema, _route_instruction(step.agent or "", labels)
                ),
            )
            if rest
            else []
        )
        for i, (good, rejected) in zip(rest, slots, strict=True):
            if good is not None:
                chosen[i] = good.get("route")
            elif rejected is not None:
                bad.append(rejected)
        counts = dict.fromkeys(labels, 0)
        branches: dict[str, Records] = {t: [] for t in step.targets}
        out: Records = []
        for i, label in enumerate(chosen):
            if label is None:
                continue
            if label not in routes:  # e.g. a cached answer from labels that changed since
                bad.append({**records[i], "_error": f"route label '{label}' is not one of {labels}"})
                continue
            counts[label] += 1
            if routes[label] == DROP:
                continue
            rec = {**records[i], "_route": label} if step.route_tag else dict(records[i])
            branches[routes[label]].append(rec)
            out.append(rec)
        self._routed[step.id] = branches
        dropped = sum(n for label, n in counts.items() if routes[label] == DROP)
        with self._spend:
            mine = result.spend[step.id]
            mine["routes"] = counts
            mine["paths"] = {"rules": len(records) - len(rest), "agent": len(rest)}
            cached = mine.get("cache_hits", 0)
        self._emit(
            run_id,
            "route.decided",
            step.id,
            counts=counts,
            rules=len(records) - len(rest),
            llm=len(rest) - cached,
            cached=cached,
            dropped=dropped,
            rejected=len(bad),
            total=len(records),
        )
        self._rejects(run_id, step.id, bad, len(records), result)
        return out

    def _pii_found(self, run_id: str, step: StepSpec, maskers: list, action: str) -> int:
        """Emits `pii.detected` with counts per field and kind (never a value); the number of
        records with personal data."""
        fields: dict[str, int] = {}
        kinds: dict[str, int] = {}
        hit = 0
        for m in maskers:
            hit += bool(m.found)
            for (f, k), n in m.found.items():
                fields[f] = fields.get(f, 0) + n
                kinds[k] = kinds.get(k, 0) + n
        if hit:
            self._emit(
                run_id,
                "pii.detected",
                step.id,
                action=action,
                records=hit,
                total=len(maskers),
                fields=fields,
                kinds=kinds,
            )
        return hit

    def _pii_gate(self, run_id: str, step: StepSpec, records: Records, result: RunResult) -> Records:
        """`pii.before_llm` warn and block (#337), before any path of an agent step. Warn only
        reports; block rejects each record with personal data to the quarantine, so it never
        reaches the llm (nor the rules of a hybrid step). Mask happens next to the call."""
        from .pii import Masker

        cfg = step.pii or PiiSpec()
        if cfg.before_llm not in ("warn", "block") or not records:
            return records
        records = list(records)
        maskers = [Masker(cfg.fields, cfg.kinds) for _ in records]
        for m, r in zip(maskers, records, strict=True):
            m.mask(r)
        if not self._pii_found(run_id, step, maskers, cfg.before_llm) or cfg.before_llm == "warn":
            return records
        keep: Records = []
        bad: Records = []
        for m, r in zip(maskers, records, strict=True):
            if not m.found:
                keep.append(r)
                continue
            where = sorted({f"{f} ({k})" for f, k in m.found})
            bad.append({**r, "_error": f"personal data in {', '.join(where)}; pii.before_llm is block"})
        self._rejects(run_id, step.id, bad, len(bad), result)
        return keep

    def _pii_masked(
        self,
        run_id: str,
        step: StepSpec,
        records: Records,
        result: RunResult,
        ask: Callable[[str, StepSpec, Records, RunResult], list[tuple[dict | None, dict | None]]],
    ) -> list[tuple[dict | None, dict | None]]:
        """`ask` (slots aligned with `records`), with `pii.before_llm: mask` (#337): each record
        goes out with placeholders, so the prompt, the cache key, the cached answer, the review
        queue and the critic only ever see those; the answer and the passed-through fields get
        the original values back afterwards."""
        cfg = step.pii
        if cfg is None or cfg.before_llm != "mask" or not records:
            return ask(run_id, step, records, result)
        from .pii import Masker

        maskers = [Masker(cfg.fields, cfg.kinds) for _ in records]
        masked = [m.mask(r) for m, r in zip(maskers, records, strict=True)]
        self._pii_found(run_id, step, maskers, "mask")
        slots = ask(run_id, step, masked, result)
        return [
            (m.restore(good) if good is not None else None, m.restore(bad) if bad is not None else None)
            for m, (good, bad) in zip(maskers, slots, strict=True)
        ]

    def _agent_reviewed(
        self, run_id: str, step: StepSpec, records: Records, result: RunResult
    ) -> list[tuple[dict | None, dict | None]]:
        """`_agent_slots`, with the human review queue (#153) when the step has `review:`. A record
        a person already reviewed takes their answer with no llm call (a rejected one is dropped);
        a new answer the review rule flags is queued, and held back or marked `_review: pending`."""
        review = step.review
        if not isinstance(review, ReviewSpec):
            return self._agent_slots(run_id, step, records, result)
        # The queue is keyed without the built-in prompt version, so reviews survive an upgrade
        # that rewords those prompts; the slots look answers up by the agent cache key.
        keys = [agent_cache_key(self.spec, step, r, prompts=False) for r in records]
        decided = self.store.review_decisions(self.spec.name, step.id, keys)
        by_cache_key = {
            agent_cache_key(self.spec, step, r): decided[k]
            for r, k in zip(records, keys, strict=True)
            if k in decided
        }
        slots = self._agent_slots(run_id, step, records, result, decided=by_cache_key)
        counts = {"queued": 0, "held": 0, "flagged": 0, "reused": 0, "dropped": 0}
        fields = list(step.output or {})
        from . import secrets

        hidden = secrets.sensitive()  # the queue keeps records like the quarantine: secrets masked
        for i, (good, _) in enumerate(slots):
            if keys[i] in decided:
                counts["reused" if decided[keys[i]]["value"] is not None else "dropped"] += 1
                continue
            if good is None:
                continue
            why = _review_reason(review, good)
            if why is None:
                continue
            answer = {k: good.get(k) for k in fields}
            shown = _mask_values(records[i], hidden) if hidden else records[i]
            if self.store.review_add(run_id, self.spec.name, step.id, keys[i], why, shown, answer):
                counts["queued"] += 1
            if review.hold:
                slots[i] = (None, None)
                counts["held"] += 1
            else:
                slots[i] = ({**good, "_review": "pending"}, None)
                counts["flagged"] += 1
        with self._spend:
            result.spend[step.id]["review"] = counts
        if counts["held"] or counts["flagged"]:
            self._emit(
                run_id,
                "review.pending",
                step.id,
                queued=counts["queued"],
                held=counts["held"],
                flagged=counts["flagged"],
            )
        if counts["reused"] or counts["dropped"]:
            self._emit(run_id, "review.reused", step.id, reused=counts["reused"], dropped=counts["dropped"])
        return slots

    def _agent_finish(
        self,
        run_id: str,
        step: StepSpec,
        slots: list[tuple[dict | None, dict | None]],
        total: int,
        result: RunResult,
    ) -> Records:
        ok: Records = []
        bad: Records = []
        for good, rejected in slots:
            if good is not None:
                ok.append(good)
            elif rejected is not None:
                bad.append(rejected)
        self._rejects(run_id, step.id, bad, total, result)
        return ok

    def _agent_slots(
        self,
        run_id: str,
        step: StepSpec,
        records: Records,
        result: RunResult,
        schema: type[BaseModel] | None = None,
        instruction: str | None = None,
        decided: dict[str, dict] | None = None,
    ) -> list[tuple[dict | None, dict | None]]:
        """Asks the llm about each record: (answered record, None) or (None, rejected record).
        `schema` and `instruction` replace the ones from `output:` and `agent:` (routers).
        `decided` maps cache keys to answers people reviewed (#153): those records take the
        person's answer, or (None, None) when they rejected it, and never reach the llm."""
        if schema is None:
            schema = llm.schema_model(f"{step.id}_output", step.output or {})
        base = (step.agent or "") if instruction is None else instruction
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
                self.store.cache_set(key(rec), out, self._ttl(step))
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

        # The tries a record gets: `attempts:` when set, else two per llm of the cascade.
        plan: list[tuple[str, str, bool]] = (
            [(a.llm or step.llm, a.validate_, a.feedback) for a in step.attempts]
            if step.attempts
            else [(name, "strict", False) for name in chain for _ in range(2)]
        )
        loose = llm.lenient_schema(schema) if any(v == "lenient" for _, v, _ in plan) else schema

        def count_attempt(n: int, name: str, usage: Any, ok: bool) -> None:
            if not step.attempts:
                return
            with self._spend:
                per = result.spend[step.id].setdefault("attempts", {})
                a = per.setdefault(
                    str(n + 1), {"llm": name, "calls": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0}
                )
                a["calls"] += 1
                a["failed"] += 0 if ok else 1
                a["input_tokens"] += getattr(usage, "input_tokens", 0)
                a["output_tokens"] += getattr(usage, "output_tokens", 0)

        def one(rec: dict) -> tuple[dict | None, dict | None]:
            last: Exception | None = None
            errors: list[dict] = []
            n = 0
            while n < len(plan):
                name, validate, feedback = plan[n]
                p, spec = self._provider(name), self.spec.llm[name]
                text = base
                if feedback and errors:
                    text = llm.feedback_instruction(text, errors)
                asked = loose if validate == "lenient" else schema
                held = self._check_budget(result, step.id)
                res: llm.Result | None = None
                try:
                    if call_tool is not None:
                        res = p.structured_tools(  # type: ignore[attr-defined]
                            text, rec, asked, tool_defs, call_tool, step.max_tool_calls
                        )
                    else:
                        res = p.structured(text, rec, asked)
                    dropped: list[dict] = []
                    if validate == "lenient":
                        out, dropped = llm.lenient_check(schema, res.value.model_dump())
                    else:
                        out = res.value.model_dump()
                except BudgetExceeded:
                    raise
                except (ValidationError, ValueError, RuntimeError) as e:
                    usage: Any = e if res is None else res  # lenient: the answer came, then failed
                    # Invalid answers still cost tokens: count them for budget and spend.
                    self._charge(result, step, spec, usage, ticket=held)
                    count_attempt(n, name, usage, False)
                    last, errors = e, llm.validation_errors(e)
                    if step.attempts:
                        self._emit(
                            run_id,
                            "agent.attempt",
                            step.id,
                            attempt=n + 1,
                            llm=name,
                            ok=False,
                            input_tokens=getattr(usage, "input_tokens", 0),
                            output_tokens=getattr(usage, "output_tokens", 0),
                            errors=errors[:10],
                        )
                    n += 1
                    continue
                assert res is not None
                self._charge(result, step, spec, res, ticket=held)
                count_attempt(n, name, res, True)
                nxt = next((j for j in range(n + 1, len(plan)) if plan[j][0] != name), None)
                if nxt is not None and unsure(out):
                    last = ValueError(f"confidence {out['confidence']} below {step.escalate_below}")
                    n = nxt  # the next llm answers this record
                    continue
                if dropped:
                    self._dropped(run_id, step.id, rec, dropped)
                return done(rec, out, name)
            return None, {**rec, "_error": str(last)}

        jury_counts = {"unanimous": 0, "majority": 0, "judged": 0, "tied": 0}

        def decide(rec: dict) -> tuple[dict | None, dict | None]:
            """`jury:` (#299): with `when: always` the jurors answer instead of the step's llm;
            with `low_confidence` only a first answer below `escalate_below` goes to the jury."""
            jury = step.jury
            if jury is None:
                return one(rec)
            if jury.when == "low_confidence":
                slot = one(rec)
                if slot[0] is None or not unsure(slot[0]):
                    return slot
            slot, how = self._jury(run_id, step, rec, schema, result, call_tool, tool_defs)
            with self._spend:
                jury_counts[how] += 1
                per = result.spend[step.id].setdefault(
                    "jury", {"unanimous": 0, "majority": 0, "judged": 0, "tied": 0}
                )
                per[how] += 1
            good = slot[0]
            if good is not None and cacheable:
                out = {k: good[k] for k in [*schema.model_fields, "_agreement"] if k in good}
                self.store.cache_set(key(rec), out, self._ttl(step))
            return slot

        batch_schema = _batch_schema(step.id, schema)

        def many(group: list[dict]) -> list[tuple[dict | None, dict | None]]:
            """One call for the whole group. Items that come back missing, duplicated or invalid
            fall back to one call per record, so a bad item never drops its neighbours."""
            outs: dict[int, dict] = {}
            held = self._check_budget(result, step.id)
            payload = {"records": [{"index": i, "record": r} for i, r in enumerate(group)]}
            try:
                res = prov.structured(_batch_instruction(base, len(group)), payload, batch_schema)
            except (ValidationError, ValueError, RuntimeError) as e:
                self._charge(result, step, llm_spec, e, ticket=held)
            else:
                self._charge(result, step, llm_spec, res, ticket=held)
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
        hits = 0
        for i, rec in enumerate(records):
            k = key(rec)
            if decided and k in decided:
                human = decided[k]["value"]
                slots[i] = ({**rec, **human}, None) if human is not None else (None, None)
            elif k in first:
                copies.append((i, first[k]))
            elif cacheable and (hit := self.store.cache_get(k)) is not None:
                slots[i] = ({**rec, **hit}, None)
                hits += 1
            else:
                first[k] = i
                todo.append(i)
        with self._spend:
            mine = result.spend[step.id]
            mine["model"] = llm_spec.model
            mine["cache_hits"] = mine.get("cache_hits", 0) + hits
        size = step.records_per_call
        groups = [todo[n : n + size] for n in range(0, len(todo), size)]
        if step.mode == "batch" and todo:
            answers_by_slot = self._run_batch(
                run_id, step, prov, schema, [records[i] for i in todo], key, result, instruction
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
                answers = pool.map(lambda g: [decide(records[g[0]])], groups)
            else:
                answers = pool.map(lambda g: many([records[i] for i in g]), groups)
            for g, answer in zip(groups, answers, strict=True):
                for i, a in zip(g, answer, strict=True):
                    slots[i] = a
        if step.jury is not None:
            self._emit(run_id, "jury.decided", step.id, **jury_counts)
        if step.critic is not None:
            answered = [(i, s[0]) for i, s in enumerate(slots) if s is not None and s[0] is not None]
            with ThreadPoolExecutor(max_workers=max(1, step.concurrency)) as pool:
                reviewed = pool.map(
                    lambda a: self._critic_loop(
                        run_id,
                        step,
                        records[a[0]],
                        a[1],
                        schema,
                        key(records[a[0]]),
                        result,
                        call_tool,
                        tool_defs,
                    ),
                    answered,
                )
                for (i, _), slot in zip(answered, reviewed, strict=True):
                    slots[i] = slot
        for i, src in copies:
            slots[i] = slots[src]
        if copies:
            self._emit(run_id, "agent.deduplicated", step.id, records=len(copies))
        assert all(slot is not None for slot in slots)
        return slots  # type: ignore[return-value]

    def _critic_loop(
        self,
        run_id: str,
        step: StepSpec,
        rec: dict,
        good: dict,
        schema: type[BaseModel],
        answer_key: str,
        result: RunResult,
        call_tool: llm.ToolCall | None,
        tool_defs: list[dict],
    ) -> tuple[dict | None, dict | None]:
        """`critic:` (#299): the reviewer llm approves the answer or sends it back with feedback.
        A rejected answer is regenerated by the step's llm with the previous answer and the
        feedback; after `max_rounds` rejections the record is quarantined. Verdicts and
        regenerations are cached, so a re-run pays nothing for them."""
        critic = step.critic
        assert critic is not None
        cacheable = self.use_cache and step.cached
        name = critic.llm or step.llm
        critic_spec = self.spec.llm[name]
        critic_key = critic_spec.model_dump(exclude=LLM_KEY_EXCLUDE)
        verdict_schema = llm.schema_model(f"{step.id}_critic", {"approved": "bool", "feedback": "str"})
        out = {k: good.get(k) for k in schema.model_fields}
        exchange: list[dict] = []
        for n in range(1, critic.max_rounds + 1):
            vkey = _hash("critic", critic.criteria, critic_key, rec, out, *_key_salt(step))
            verdict = self.store.cache_get(vkey) if cacheable else None
            cached = verdict is not None
            if verdict is None:
                held = self._check_budget(result, step.id)
                payload = {"record": rec, "answer": out, "criteria": critic.criteria}
                try:
                    res = self._provider(name).structured(
                        _critic_instruction(critic.criteria), payload, verdict_schema
                    )
                except (ValidationError, ValueError, RuntimeError) as e:
                    self._charge(result, step, critic_spec, e, ticket=held)
                    return None, {**rec, "_error": f"critic did not answer in the expected shape: {e}"}
                self._charge(result, step, critic_spec, res, ticket=held)
                verdict = res.value.model_dump()
                if cacheable:
                    self.store.cache_set(vkey, verdict, self._ttl(step))
            approved, feedback = bool(verdict["approved"]), str(verdict["feedback"] or "")
            self._count_critic(result, step.id, "approved" if approved else "rejected")
            self._emit(
                run_id,
                "critic.round",
                step.id,
                round=n,
                llm=name,
                approved=approved,
                cached=cached,
                feedback=feedback if len(feedback) <= 300 else feedback[:300] + "…",
            )
            if approved:
                return {**rec, **out}, None
            exchange.append({"round": n, "answer": out, "feedback": feedback})
            if n == critic.max_rounds:
                break
            # Regenerate with the previous answer and the critic's feedback.
            rkey = _hash("critic-regen", answer_key, out, feedback)
            hit = self.store.cache_get(rkey) if cacheable else None
            if hit is not None:
                out = hit
                continue
            instruction = _regenerate_instruction(step.agent or "", out, feedback)
            gen_spec = self.spec.llm[step.llm]
            held = self._check_budget(result, step.id)
            try:
                prov = self._provider(step.llm)
                if call_tool is not None:
                    res = prov.structured_tools(  # type: ignore[attr-defined]
                        instruction, rec, schema, tool_defs, call_tool, step.max_tool_calls
                    )
                else:
                    res = prov.structured(instruction, rec, schema)
            except (ValidationError, ValueError, RuntimeError) as e:
                self._charge(result, step, gen_spec, e, ticket=held)
                self._count_critic(result, step.id, "quarantined")
                return None, {
                    **rec,
                    "_error": f"regeneration after critic feedback failed: {e}",
                    "_critic": exchange,
                }
            self._charge(result, step, gen_spec, res, ticket=held)
            out = res.value.model_dump()
            if cacheable:
                self.store.cache_set(rkey, out, self._ttl(step))
        self._count_critic(result, step.id, "quarantined")
        last = exchange[-1]["feedback"] if exchange else ""
        error = f"critic did not approve after {critic.max_rounds} rounds: {last}"
        return None, {**rec, "_error": error, "_critic": exchange}

    def _jury(
        self,
        run_id: str,
        step: StepSpec,
        rec: dict,
        schema: type[BaseModel],
        result: RunResult,
        call_tool: llm.ToolCall | None,
        tool_defs: list[dict],
    ) -> tuple[tuple[dict | None, dict | None], str]:
        """Each juror answers the record on its own; then a majority on `fields` or a judge picks
        the answer. Returns the slot and how it was decided (unanimous, majority, judged, tied).
        Juror and judge answers are cached, so a re-run pays nothing for them."""
        jury = step.jury
        assert jury is not None
        cacheable = self.use_cache and step.cached
        instruction = step.agent or ""

        def ask(name: str, kind: str, text: str, payload: dict) -> dict | None:
            spec = self.spec.llm[name]
            dumped = spec.model_dump(exclude=LLM_KEY_EXCLUDE)
            ckey = _hash(kind, dumped, text, step.output, step.tools, payload, *_key_salt(step))
            hit = self.store.cache_get(ckey) if cacheable else None
            if hit is not None:
                return hit
            held = self._check_budget(result, step.id)
            try:
                prov = self._provider(name)
                if call_tool is not None:
                    res = prov.structured_tools(  # type: ignore[attr-defined]
                        text, payload, schema, tool_defs, call_tool, step.max_tool_calls
                    )
                else:
                    res = prov.structured(text, payload, schema)
            except (ValidationError, ValueError, RuntimeError) as e:
                self._charge(result, step, spec, e, ticket=held)
                return None
            self._charge(result, step, spec, res, ticket=held)
            out: dict = res.value.model_dump()
            if cacheable:
                self.store.cache_set(ckey, out, self._ttl(step))
            return out

        answers = {name: ask(name, "jury", instruction, rec) for name in jury.llms}
        votes = {n: a for n, a in answers.items() if a is not None}
        fields = jury.fields or list(schema.model_fields)
        groups: dict[str, list[str]] = {}
        for n, a in votes.items():
            groups.setdefault(_hash([_norm(a.get(f)) for f in fields]), []).append(n)
        sizes = sorted((len(g) for g in groups.values()), reverse=True)
        top = sizes[0] if sizes else 0
        agreement = round(top / len(jury.llms), 2)
        winner = next((g for g in groups.values() if len(g) == top), [])
        clear = top >= 2 and (len(sizes) == 1 or sizes[1] < top)
        how = "unanimous" if top == len(jury.llms) else "majority"
        out: dict | None = votes[winner[0]] if clear else None
        if jury.decide == "judge" and how != "unanimous":
            how = "judged"
            payload = {"record": rec, "answers": [{"juror": n, "answer": a} for n, a in votes.items()]}
            assert jury.judge is not None
            out = ask(jury.judge, "jury-judge", _judge_instruction(instruction), payload) if votes else None
            if out is None:
                how = "tied"
        elif not clear:
            how = "tied"
        if out is None:
            failed = [n for n, a in answers.items() if a is None]
            why = "the judge gave no valid answer" if jury.decide == "judge" else "no answer has a majority"
            error = f"jury: {why} on {', '.join(fields)}"
            if failed:
                error += f" (no valid answer from {', '.join(failed)})"
            return (None, {**rec, "_error": error, "_jury": votes}), "tied"
        good = {**rec, **out}
        if step.jury_tag:
            good["_agreement"] = agreement
        return (good, None), how

    def _count_critic(self, result: RunResult, step_id: str, what: str) -> None:
        with self._spend:
            per = result.spend[step_id].setdefault("critic", {"approved": 0, "rejected": 0, "quarantined": 0})
            per[what] += 1

    def _dropped(self, run_id: str, step_id: str, rec: dict, dropped: list[dict]) -> None:
        """A lenient attempt kept the record but dropped invalid values: each goes to the
        quarantine with its reason (field, rule, value), and an `item.dropped` event says so."""
        rows = [
            {**rec, "_error": f"dropped {d['field']}: {d['rule']}", "_dropped": True, "_value": d["value"]}
            for d in dropped
        ]
        self._quarantine(run_id, step_id, rows)
        self._emit(run_id, "item.dropped", step_id, count=len(rows), error=rows[0]["_error"])

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

    def _step_input(self, step: StepSpec, outputs: dict[str, Records]) -> Any:
        """What a step reads: its one input, a dict of inputs, or None for a source."""
        refs = step.inputs
        whole = outputs.raw if isinstance(outputs, Outputs) else outputs.__getitem__

        def raw(ref: str) -> Any:  # a router's target reads only its branch (#299)
            branch = self._routed.get(ref)
            return branch[step.id] if branch is not None and step.id in branch else whole(ref)

        if step.id in self.replay:
            return self.replay[step.id]
        if len(refs) == 1:
            return raw(refs[0])
        if refs:
            return {r: raw(r) if r in self._routed else outputs[r] for r in refs}
        return None

    def _run_step(
        self,
        run_id: str,
        step: StepSpec,
        outputs: dict[str, Records],
        result: RunResult,
        data: Any = _ALL,
    ) -> Records:
        """Runs one step body. `data` replaces its input (one series of an isolated step)."""
        refs = step.inputs
        whole = outputs.raw if isinstance(outputs, Outputs) else outputs.__getitem__

        def raw(ref: str) -> Any:  # a router's target reads only its branch (#299)
            branch = self._routed.get(ref)
            return branch[step.id] if branch is not None and step.id in branch else whole(ref)

        if data is _ALL:
            data = self._step_input(step, outputs)

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

        if step.use == "cache":
            from .connectors.transforms import cache_step

            with_ = dict(step.with_)
            if isinstance(with_.get("key"), str):
                with_["key"] = with_["key"].replace("{pipeline}", self.spec.name)
            return cache_step(self.store, data, base_dir=self.spec.base_dir, **with_)

        if step.use == "sql":
            # Each input is a table named after its step; Arrow outputs are passed as they are.
            from .connectors import duck

            if step.id in self.replay:
                tables = data if isinstance(data, dict) else {(refs or ["input"])[0]: data}
            else:
                tables = {r: raw(r) for r in refs}
            known = {}  # an empty input keeps the columns its step declares (#288)
            for name, rows in tables.items():
                src = next((s for s in self.spec.steps if s.id == name), None)
                if rows is not None and not _is_table(rows) and not rows and src is not None:
                    cols = getattr(rows, "columns", None) or list(src.schema_ or src.output or {})
                    if cols:
                        known[name] = cols
            return duck.run(step.id, tables, base_dir=self.spec.base_dir, columns=known, **step.with_)
        if step.use == "map_schema":
            return self._run_map_schema(run_id, step, data, result)

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
        seen = registry._SEEN.set(self._seen_keys())
        store = registry._RUN_STORE.set(self.store)
        try:
            out = fn(data, **step.with_)
        finally:
            registry._RUN_STORE.reset(store)
            registry._SEEN.reset(seen)
            registry._STEP_STATE.reset(state)
            registry._AFTER_RUN.reset(token)
        if isinstance(out, registry.Rejects) and out.rejected:
            self._rejects(run_id, step.id, out.rejected, _seen(out), result)
        if not isinstance(out, (registry.EmptyRows, registry.Changes)):  # columns (#288), changes summary
            out = list(out) if out is not None else []
        if self.use_cache and step.cached:
            self.store.cache_set(key, out, self._ttl(step))
        return out

    @staticmethod
    def _ttl(step: StepSpec) -> float | None:
        return None if step.cache_ttl is None else checks.parse_duration(step.cache_ttl)

    def _run_map_schema(self, run_id: str, step: StepSpec, data: Any, result: RunResult) -> Records:
        """Map columns with the mapping stored for this layout; infer it (by name, then one LLM
        call) only for a layout not seen yet. See agentl/mapping.py."""
        from . import mapping

        if isinstance(data, dict):
            raise ValueError("map_schema steps take a single input")
        llm_spec = self.spec.llm[step.llm]

        def ask(instruction: str, record: dict, schema: type) -> Any:
            held = self._check_budget(result, step.id)
            try:
                res = self._provider(step.llm).structured(instruction, record, schema)
            except (ValidationError, ValueError, RuntimeError) as e:
                self._charge(result, step, llm_spec, e, ticket=held)
                raise RuntimeError(f"map_schema: the LLM did not answer with a mapping: {e}") from e
            self._charge(result, step, llm_spec, res, ticket=held)
            return res.value

        name = self.spec.name
        params = dict(step.with_)
        pending = self._remap.pop(step.id, [])
        if pending:
            # a check upstream found a new layout and hands it here: infer a mapping for it (or
            # reuse one stored for that layout, or the pinned `mapping:`) instead of failing
            params["on_drift"] = "infer"
        out, new = mapping.run(
            list(data or []),
            load=lambda: self.store.mapping_get(name, step.id),
            save=lambda value: self.store.mapping_set(name, step.id, value),
            ask=ask,
            where=f"{name} {step.id}",
            **params,
        )
        for memory, layout in pending:  # mapped: the new layout becomes the one on record
            self.store.cache_set(memory, layout)
        if new is not None:
            self._emit(
                run_id,
                "mapping.inferred",
                step.id,
                signature=new["signature"],
                columns=new["columns"],
                by_llm=new["by_llm"],
                approved=new["approved"],
            )
        if out.rejected:
            self._rejects(run_id, step.id, out.rejected, len(out) + len(out.rejected), result)
        return list(out)

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
            held = self._check_budget(result, step.id)
            prov, spec = self._provider(step.llm), self.spec.llm[step.llm]
            try:
                out = prov.structured(instruction, record, res.Choice)
            except llm.InvalidOutput as e:
                self._charge(result, step, spec, e, ticket=held)
                return None, e
            self._charge(result, step, spec, out, ticket=held)
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
    def _run_check(self, run_id: str, step: StepSpec, records: Any, result: RunResult) -> Records:
        """Fixed expectations, then the optional agent review of the data's profile. Emits
        `check.passed` or `check.failed`; with `on_fail: stop` a failure stops the step before the
        data reaches the next one (usually the writer). The profile of each passing run is kept to
        compare the next run against."""
        if step.on_drift:
            self._check_drift(run_id, step, records)
        if _is_table(records):
            records = records.to_pylist()
        records = list(records)
        rule_findings: list[dict] = []
        if step.rules:
            records, rule_findings = self._run_rules(run_id, step, records, result)
        if not (step.expect or step.review or step.rules):
            return records
        memory = _hash("check-profile", self.spec.name, step.id)
        previous = self.store.cache_get(memory)
        history_key = _hash("check-history", self.spec.name, step.id)
        history = self.store.cache_get(history_key) or [] if "vs_runs" in step.expect else []
        with self._spend:
            stats = {k: (v[0], v[1]) for k, v in self._reject_stats.items()}
        findings = checks.run_expectations(
            records,
            step.expect,
            previous,
            rejected=stats,
            step=step.check or "",
            history=history,
            protected=self._protected_fields(),
        )
        rules = {k: "passed" for k in step.expect}
        for f in findings:
            f["severity"] = step.severity.get(f["check"], "fail")
            rules[f["check"]] = f["severity"] if f["severity"] != "fail" else "failed"
        rejecting = {f["check"] for f in findings if f["severity"] == "reject"}
        if rejecting:
            bad: dict[int, str] = {}
            for key in step.expect:
                if key in rejecting:
                    for i, why in checks.failing_rows(
                        records, key, step.expect[key], self._protected_fields()
                    ).items():
                        bad.setdefault(i, why)
            dropped = [{**records[i], "_error": bad[i]} for i in sorted(bad)]
            total = len(records)
            records = [r for i, r in enumerate(records) if i not in bad]
            self._rejects(run_id, step.id, dropped, total, result)
        warnings = [f for f in findings if f["severity"] == "warn"]
        if warnings:
            self._emit(run_id, "check.warned", step.id, rows=len(records), findings=warnings)
        findings = [f for f in findings if f["severity"] == "fail"]
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
            held = self._check_budget(result, step.id)
            try:
                res = self._provider(step.llm).structured(
                    _review_instruction(str(step.review)), payload, schema
                )
            except (ValidationError, ValueError, RuntimeError) as e:
                self._charge(result, step, llm_spec, e, ticket=held)
                raise RuntimeError(f"check review did not answer in the expected shape: {e}") from e
            self._charge(result, step, llm_spec, res, ticket=held)
            verdict = res.value.model_dump()
            if verdict["passed"]:
                notes = list(verdict["findings"])
                rules["review"] = "passed"
            else:
                found = [{"check": "review", "detail": f} for f in verdict["findings"]] or [
                    {"check": "review", "detail": "the reviewer rejected the data without a reason"}
                ]
                level = step.severity.get("review", "fail")
                for f in found:
                    f["severity"] = level
                rules["review"] = "failed" if level == "fail" else level
                if level == "fail":
                    findings += found
                else:
                    self._emit(run_id, "check.warned", step.id, rows=len(records), findings=found)
        fatal = [f for f in rule_findings if f["action"] == "fail"]
        if fatal:
            findings += [{"check": f["rule"], "detail": f["detail"], "severity": "fail"} for f in fatal]
        data = {
            "rows": len(records),
            "findings": findings,
            "rules": rules,
            **({"notes": notes} if notes else {}),
        }
        if findings:
            self._emit(run_id, "check.failed", step.id, on_fail=step.on_fail, **data)
            if step.on_fail == "stop" or fatal:
                first = "; ".join(f["detail"] for f in findings[:3])
                more = f" (+{len(findings) - 3} more)" if len(findings) > 3 else ""
                raise CheckFailed(f"{len(findings)} problem(s): {first}{more}")
        else:
            self._emit(run_id, "check.passed", step.id, **data)
            kept = {k: v for k, v in prof.items() if k != "sample"}
            self.store.cache_set(memory, kept)
            if "vs_runs" in step.expect:
                self.store.cache_set(history_key, [kept, *history][: checks.HISTORY])
        return records

    def _protected_fields(self) -> set[str]:
        """Fields a `protect` step in this pipeline marks: check messages never show their values."""
        out: set[str] = set()
        for s in self.spec.steps:
            if s.use == "protect" and isinstance(s.with_.get("fields"), dict):
                out.update(str(f) for f in s.with_["fields"])
        return out

    def _check_drift(self, run_id: str, step: StepSpec, data: Any) -> None:
        """Compares the input's layout with the one recorded for this pipeline and step (#178).
        The first run records it. On drift emits `schema.drift`; `warn` accepts the new layout,
        `fail` stops, `remap` passes the data on and hands the new layout to the map_schema steps
        that read this check: they infer (or reuse) a mapping for it, and the new layout is put on
        record only once they have mapped it. An empty input says nothing about the layout and is
        skipped."""
        if not _is_table(data) and not data:
            return
        memory = _hash("schema-signature", self.spec.name, step.id)
        now = drift.signature(data)
        before = self.store.cache_get(memory)
        if before is None:
            self.store.cache_set(memory, now)
            return
        d = drift.diff(before, now)
        if d is None:
            return
        layout: dict[str, Any] = {}
        if step.on_drift == "remap":
            targets = remap_targets(self.spec, step.id)
            layout = {"previous": before, "current": now, "remap": targets}
            for t in targets:
                self._remap.setdefault(t, []).append((memory, now))
        self._emit(run_id, "schema.drift", step.id, source=step.check, on_drift=step.on_drift, **d, **layout)
        if step.on_drift == "warn":
            self.store.cache_set(memory, now)
            return
        if step.on_drift == "remap":
            return  # the map_schema steps reading this check map the new layout
        raise SchemaDrift(f"'{step.check}' changed layout: {drift.summary(d)}")

    def _run_rules(
        self, run_id: str, step: StepSpec, records: Records, result: RunResult
    ) -> tuple[Records, list[dict]]:
        """Per-row rules (#218): drop, flag or fail rows; then the daily digest of the findings
        against the previous day's bucket, emitted as `check.digest` even on a clean day."""
        kept, found, errors = checks.run_rules(records, step.rules, step.identity)
        for err in errors:
            log.warning("check %s: rule error (row passed): %s", step.id, err)
        kept_ids = {id(r) for r in kept}
        dropped = [r for r in records if id(r) not in kept_ids]
        if dropped:
            bad = [{**r, "_error": "dropped by a check rule"} for r in dropped]
            self._rejects(run_id, step.id, bad, len(records), result)
        memory = _hash("check-digest", self.spec.name, step.id)
        bucket = self.store.cache_get(memory) or {}
        day = _today()
        ids = checks.digest_ids(found)
        if bucket.get("day") == day:
            ids = sorted(set(ids) | set(bucket.get("ids", [])))
            prev = bucket.get("prev", [])
        else:
            prev = bucket.get("ids", [])
        self.store.cache_set(memory, {"day": day, "ids": ids, "prev": prev})
        diff = checks.digest(ids, prev)
        cap = 50
        self._emit(
            run_id,
            "check.digest",
            step.id,
            day=day,
            all_clear=not ids,
            new=len(diff["new"]),
            resolved=len(diff["resolved"]),
            ongoing=len(diff["ongoing"]),
            dropped=len(dropped),
            flagged=sum(1 for f in found if f["action"] == "flag"),
            rule_errors=len(errors),
            summary=_digest_text(diff, cap) if ids or prev else "all clear: no findings today",
            sections={k: v[:cap] for k, v in diff.items()},
        )
        return kept, found

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
        a check, or a `@dataframe` fn with that single input). Cached and chunked steps keep records."""
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
            if step.check:  # reads the Table's schema for drift, then hands on records
                return True
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
        if step.kind == "agent" or step.review or step.use in ("map_schema", "resolve"):
            with self._spend:
                result.spend[step_id] = dict.fromkeys(
                    ("input_tokens", "output_tokens", "cache_read_tokens", "llm_calls", "tool_calls"), 0
                ) | {"cost_usd": 0.0}
        self._emit(run_id, "step.started", step_id, kind=step.kind)
        t0 = time.time()
        hooks_before = hooks_after = len(self._after_run)
        try:
            if step.cursor:
                step = self._with_cursor(step)
            try:
                out = self._run_resilient(run_id, step, result)
            finally:
                self._release_step(step_id)
            if step.schema_ is not None:
                out = self._contract(run_id, step, out, result)
            hooks_after = len(self._after_run)
            self._write_rejected(step, result)
            if step.cursor:
                self._track_cursor(step, out)
        except registry.NoChange as e:
            self._emit(run_id, "step.unchanged", step_id, reason=str(e))
            raise
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
                traceback=_trace(e),
                **({} if step.required else {"required": False}),
                **self._step_spend(step, result),
            )
            if not step.required and not isinstance(e, BudgetExceeded):
                with self._spend:
                    result.best_effort_failed[step_id] = _describe(e)
                result.outputs[step_id] = []
                return
            raise
        result.outputs[step_id] = out
        self._sample(run_id, step_id, out)
        written, counts = _write_counts(out)
        if counts:
            result.writes[step_id] = counts
        self._emit(
            run_id,
            "step.succeeded",
            step_id,
            # a writer returns one summary record per file or table: report the rows it wrote
            records=written if written else len(out),
            **({"written": written} if written else {}),
            **counts,
            seconds=round(time.time() - t0, 3),
            **self._step_spend(step, result),
        )
        self._checkpoint(run_id, step, out, hooks_after > hooks_before)
        if step.persist == "raw":
            self._persist_raw(run_id, step, out)
        if len(out) == 0 and not step.inputs:
            self._emit(run_id, "source.empty", step_id)

    def _prune_raw(self, run_id: str) -> None:
        """Apply `raw.keep_days` after a run that keeps originals (#208)."""
        keep = self.spec.raw.keep_days if self.spec.raw else None
        if keep is None or not any(s.persist == "raw" for s in self.spec.steps):
            return
        from . import raw

        try:
            manifests, objects = raw.prune(raw.root(self.spec, self.store), keep)
        except OSError as e:
            log.warning("raw zone: retention failed: %s", e)
            return
        if manifests or objects:
            self._emit(run_id, "raw.pruned", manifests=manifests, objects=objects, keep_days=keep)

    def _persist_raw(self, run_id: str, step: StepSpec, out: Any) -> None:
        """Keep the output of a `persist: raw` step as an immutable original (#208)."""
        from . import raw

        reason = "decrypted values are never stored" if step.use == "unprotect" else None
        manifest = None
        if reason is None:
            try:
                manifest, reason = raw.store_output(self.spec, self.store, run_id, step.id, out)
            except OSError as e:
                reason = f"could not write the original: {e}"
        if manifest is None:
            log.warning("raw zone: step %s not stored: %s", step.id, reason)
            self._emit(run_id, "raw.skipped", step.id, reason=reason)
            return
        self._emit(
            run_id,
            "raw.stored",
            step.id,
            sha256=manifest["sha256"],
            bytes=manifest["bytes"],
            records=manifest["records"],
        )

    def _checkpoint(self, run_id: str, step: StepSpec, out: Any, acked: bool = False) -> None:
        """Record that `step` completed, keeping its output when that is cheap and safe (#339)."""
        from . import resume

        reason = None
        if not resume.enabled():
            reason = "checkpoints are off (AGENTL_CHECKPOINTS=0)"
        elif step.use == "unprotect":
            reason = "decrypted values are never stored"
        elif step.cursor:
            reason = "incremental cursor step: it reads again from the stored cursor"
        elif step.routes is not None:
            reason = "router step: its branches are not stored"
        elif acked:
            reason = "source acknowledged after the run: it reads again"
        try:
            resume.record(self.store, run_id, step.id, out, reason)
        except Exception as e:  # a checkpoint never fails a run
            log.warning("checkpoint of step %s failed: %s", step.id, e)

    def _run_retried(self, run_id: str, step: StepSpec, result: RunResult, data: Any = _ALL) -> Any:
        """Runs a step body, retrying it on the errors its `retries.on` lists (#296). A budget stop
        or NoChange is never retried; a cached result returns without running anything. SQL
        writers without `retries` retry deadlocks and unique-key races (`default_retries`)."""
        r = step.retries or default_retries(step)
        attempts = r.attempts if r else 1
        for attempt in range(1, attempts + 1):
            try:
                return self._run_step(run_id, step, result.outputs, result, data)
            except (BudgetExceeded, registry.NoChange):
                raise
            except Exception as e:
                if r is None or attempt == attempts or not _retry_matches(e, list(r.on)):
                    raise
                cap = checks.parse_duration(r.max_backoff)
                wait = min(cap, checks.parse_duration(r.backoff) * 2 ** (attempt - 1)) * _jitter()
                asked = _retry_after(e)
                if asked is not None:
                    wait = min(cap, max(wait, asked))
                self._emit(
                    run_id, "step.retry", step.id, attempt=attempt, error=_describe(e), wait=round(wait, 3)
                )
                _sleep(wait)
        raise AssertionError("unreachable")

    def _run_isolated(self, run_id: str, step: StepSpec, result: RunResult) -> Any:
        """`isolate: per_series` (#206): the step (with its retries and fallbacks) once per group of
        `series` values. A failed group emits `step.series_failed` and is recorded in
        `RunResult.series_failed`; the others still run. Fails only when every group failed."""
        data = self._step_input(step, result.outputs)
        if _is_table(data):
            data = data.to_pylist()
        groups: dict[str, list[Any]] = {}
        for r in data or []:
            values = [r.get(c) if isinstance(r, dict) else None for c in step.series]
            label = (
                str(values[0])
                if len(values) == 1
                else ",".join(f"{c}={v}" for c, v in zip(step.series, values, strict=True))
            )
            groups.setdefault(label, []).append(r)
        if not groups:
            return self._run_resilient(run_id, step, result, data)
        out: list[Any] = []
        failed: dict[str, str] = {}
        first: Exception | None = None
        for label, rows in groups.items():
            try:
                got = self._run_resilient(run_id, step, result, rows)
            except (BudgetExceeded, registry.NoChange):
                raise
            except Exception as e:
                first = first or e
                failed[label] = _describe(e)
                self._emit(
                    run_id, "step.series_failed", step.id, series=label, rows=len(rows), error=failed[label]
                )
                continue
            out.extend(got.to_pylist() if _is_table(got) else got)
        self._emit(run_id, "step.isolated", step.id, series=len(groups), failed=len(failed))
        if first is not None and len(failed) == len(groups):
            raise first
        if failed:
            with self._spend:
                result.series_failed[step.id] = failed
        return out

    def _run_resilient(self, run_id: str, step: StepSpec, result: RunResult, data: Any = _ALL) -> Any:
        """The step with its retries, then each `fallback` body in order with the same input."""
        if step.isolate and data is _ALL:
            return self._run_isolated(run_id, step, result)
        if not step.fallback:
            return self._run_retried(run_id, step, result, data)
        try:
            out, source = self._run_retried(run_id, step, result, data), "main"
        except (BudgetExceeded, registry.NoChange):
            raise
        except Exception as e:
            error, came_from, out, source = e, step.use or step.fn, None, ""
            for i, body in enumerate(step.fallback):
                self._emit(
                    run_id,
                    "step.fallback",
                    step.id,
                    **{"from": came_from},
                    to=body.use,
                    error=_describe(error),
                )
                alt = step.model_copy(
                    update={
                        "use": body.use,
                        "fn": None,
                        "with_": dict(body.with_),
                        "fallback": [],
                        "retries": None,
                    }
                )
                try:
                    out = self._run_step(run_id, alt, result.outputs, result, data)
                    source = f"fallback[{i}]"
                    break
                except (BudgetExceeded, registry.NoChange):
                    raise
                except Exception as e2:
                    error, came_from = e2, body.use
            if not source:
                raise error from None
            with self._spend:
                result.fallbacks[step.id] = source
        if step.fallback_tag:
            if _is_table(out):
                import pyarrow as pa

                out = out.append_column("_source", pa.array([source] * out.num_rows, pa.string()))
            else:
                out = [{**r, "_source": source} if isinstance(r, dict) else r for r in out]
        return out

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
        gates = self._gates()
        for head in self.spec.steps:
            if not head.use or head.depends or head.cursor or head.persist:
                continue  # a `persist: raw` step keeps its whole output, so it is a boundary
            if registry.streaming(head.use)[0] is None:
                continue
            chain, cur = [head], head
            while len(consumers[cur.id]) == 1:
                nxt = consumers[cur.id][0]
                if (
                    nxt.depends != [cur.id]
                    or not nxt.required
                    or nxt.routes is not None
                    or nxt.id in gates
                    or nxt.isolate
                    or nxt.persist
                ):
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
                    wrote = writer(chunks, **last.with_)
                except Exception:
                    failing.append(last.id)
                    raise
                if isinstance(wrote, registry.Rejects) and wrote.rejected:
                    self._rejects(run_id, last.id, wrote.rejected, _seen(wrote), result)
                out = wrote if isinstance(wrote, registry.Changes) else list(wrote or [])
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
                trace = {"traceback": _trace(e)} if step.id == culprit else {}
                self._emit(
                    run_id, "step.failed", step.id, error=error, **trace, **self._step_spend(step, result)
                )
            raise
        result.outputs[last.id] = out
        seconds = round(time.time() - t0, 3)
        wrote_n, upserts = _write_counts(out) if writer is not None else (0, {})
        if upserts:
            result.writes[last.id] = upserts
        for step in chain:
            extra: dict[str, Any] = {}
            if step is last and wrote_n:
                counts[step.id] = wrote_n
                extra = {"written": wrote_n, **upserts}
            self._emit(
                run_id,
                "step.succeeded",
                step.id,
                records=counts[step.id],
                **extra,
                seconds=seconds,
                chunked=True,
                **self._step_spend(step, result),
            )
            if step is last:
                self._checkpoint(run_id, step, out)
            else:
                from . import resume

                resume.record(self.store, run_id, step.id, [], "inside a chunked chain")
        if counts[head.id] == 0:
            self._emit(run_id, "source.empty", head.id)

    def _gates(self) -> dict[str, set[str]]:
        """A required check with `on_fail: stop` gates the run's outputs (#278): every sink step (one
        no other step depends on, usually a writer) that is not a check waits for those checks, so
        a failing check stops a sibling writer instead of racing it."""
        stops = {s.id for s in self.spec.steps if s.check and s.on_fail == "stop" and s.required}
        if not stops:
            return {}
        used = {d for s in self.spec.steps for d in s.depends}
        return {s.id: stops for s in self.spec.steps if s.id not in used and not s.check}

    def _run_graph(self, run_id: str, result: RunResult) -> None:
        """Starts each step as soon as its inputs are done, up to `max_parallel_steps` at once,
        picking ready steps in declaration order. After a failure no new step starts; the ones
        already running finish, then the first error is raised."""
        chains = self._chains()
        self._arrow_out = self._arrow_plan(chains)
        inside = {s.id for chain in chains.values() for s in chain[1:]}
        pending = [i for i in graph.order(self.spec) if i not in inside]
        deps = {s.id: set(s.depends) for s in self.spec.steps}
        for sink, checks_ in self._gates().items():  # writers wait for `on_fail: stop` checks (#278)
            deps[sink] |= checks_
        limit = self.spec.max_parallel_steps
        done: set[str] = set()
        if self._plan is not None:
            for step_id, out in self._plan.restored.items():
                result.outputs[step_id] = out
            for step_id in self._plan.done:
                result.outputs[step_id] = []
            done = set(self._plan.restored) | self._plan.done
            pending = [i for i in pending if i not in done]
        running: dict[Future, str] = {}
        failure: BaseException | None = None
        with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="agentl-step") as pool:
            while pending or running:
                if failure is None:
                    for step_id in [i for i in pending if deps[i] <= done][: limit - len(running)]:
                        pending.remove(step_id)
                        if step_id in chains:
                            future = pool.submit(
                                contextvars.copy_context().run,
                                self._run_chain,
                                run_id,
                                chains[step_id],
                                result,
                            )
                        else:
                            future = pool.submit(
                                contextvars.copy_context().run,
                                self._run_one,
                                run_id,
                                self.spec.step(step_id),
                                result,
                            )
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
        c = self.spec.concurrency
        if c is None:
            return self._run(run_id)
        key = "run:" + c.key.replace("{pipeline}", self.spec.name)
        lease, wait_s = checks.parse_duration(c.lease), checks.parse_duration(c.wait)
        deadline = time.monotonic() + wait_s
        pause = 0.05
        while True:
            with self.store.lease(key, lease, owner=run_id) as held:
                if held:
                    return self._run(run_id)
            if c.on_overlap != "queue" or time.monotonic() > deadline:
                break
            time.sleep(pause)
            pause = min(pause * 2, 5.0)
        holder = self.store.lock_holder(key)
        other = holder["owner"] if holder else "another run"
        if c.on_overlap == "skip":
            return self._not_run(run_id, "skipped", f"{other} holds concurrency key {key[4:]!r}")
        why = f"{other} holds concurrency key {key[4:]!r}"
        if c.on_overlap == "queue":
            why += f" after waiting {wait_s:.0f}s"
        return self._not_run(run_id, "failed", why)

    def _not_run(self, run_id: str, status: str, reason: str) -> RunResult:
        """A run that did not start because another one holds its concurrency key."""
        result = RunResult(run_id=run_id, status=status, error=reason)
        self.store.start_run(run_id, self.spec.name, graph.mermaid(self.spec), run_labels.of(self.spec))
        self.store.finish_run(run_id, status, reason)
        self._emit(run_id, f"run.{status}", seconds=0.0, reason=reason)
        return result

    def _run(self, run_id: str) -> RunResult:
        result = RunResult(run_id=run_id, status="running")
        self._run_id = run_id
        self._result = result
        self._failed_step = None
        self._outputs = result.outputs
        self._source_records = {}
        registry._RUN_ID.set(run_id)
        from . import resume

        self._plan = None
        if self.resume_from:  # refused before anything is recorded
            self._plan = resume.plan(self.spec, self.store, self.resume_from, self._chains())
        elif self.raw_from:
            from . import raw

            self._plan = raw.plan(self.spec, self.store, self.raw_from)
        self.store.start_run(run_id, self.spec.name, graph.mermaid(self.spec), run_labels.of(self.spec))
        resumed: dict[str, Any] = {}
        if self.raw_from and self._plan is not None:
            resumed = {"replayed_from_raw": self.raw_from}
        elif self._plan is not None:
            resumed = {"resumed_from": self._plan.resumed_from, "resumed_chain": self._plan.ancestors}
        self._emit(
            run_id,
            "run.started",
            steps=len(self.spec.steps),
            spec_hash=resume.spec_hash(self.spec),
            **resumed,
        )
        if self.raw_from and self._plan is not None:
            self._emit(
                run_id,
                "run.replayed",
                from_raw=self.raw_from,
                originals=self._plan.checkpoints,
                skipped=sorted(self._plan.done),
            )
        elif self._plan is not None:
            for step_id, data in self._plan.checkpoints.items():  # so this run can be resumed too
                self.store.event(run_id, "checkpoint", step_id, data)
            self._emit(
                run_id,
                "run.resumed",
                resumed_from=self._plan.resumed_from,
                skipped=sorted(self._plan.restored) + sorted(self._plan.done),
                rerun=self._plan.rerun,
            )
        started = time.time()
        failure: BaseException | None = None
        try:
            self._run_graph(run_id, result)
            result.status = "succeeded"
        except registry.NoChange as e:
            result.status = "no_change"
            result.error = None
            self._emit(run_id, "source.unchanged", reason=str(e))
        except Exception as e:
            result.status = "failed"
            result.error = _describe(e)
            failure = e
        finally:
            if self._toolbox is not None:  # MCP servers started for this run
                self._toolbox.close()
                self._toolbox = None
        skip = self.spec.outcomes.on_transport_error == "skipped_upstream"
        if failure is not None and skip and is_transport_error(failure):
            result.status = "skipped"
            result.reason = f"source unreachable: {result.error}"
        self._finish_after_run(run_id, result)
        self._save_cursors(run_id, result)
        if result.status in ("succeeded", "no_change"):  # nothing left to resume
            resume.remove(self.store, [run_id, *(self._plan.ancestors if self._plan else [])])
        self._prune_raw(run_id)
        partial: dict[str, Any] = {}
        if result.partial:
            partial = {"partial": True, "best_effort_failed": sorted(result.best_effort_failed)}
            if result.series_failed:
                partial["series_failed"] = {k: sorted(v) for k, v in result.series_failed.items()}
        self._classify(result)
        self.store.finish_run(run_id, result.status, result.error, result.outcome)
        # before the final event, so run.<status> stays the last event of a run
        fault = self._track_error(run_id, result)
        if result.outcome in self.spec.outcomes.alert_on:
            extra = {"error": result.error} if result.error else {}
            self._emit(run_id, "run.alert", outcome=result.outcome, **extra, **fault)
        throttled: dict[str, Any] = (
            {"throttled_s": round(result.throttled_s, 3)} if result.throttled_s else {}
        )
        self._emit(
            run_id,
            f"run.{result.status}",
            outcome=result.outcome,
            seconds=round(time.time() - started, 3),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=round(result.cost_usd, 6),
            **throttled,
            rejected=sum(len(v) for v in result.rejected.values()),
            **partial,
            **({"reason": result.reason} if result.reason else {}),
            **({"error": result.error} if result.error else {}),
            **fault,
        )
        return result

    def _track_error(self, run_id: str, result: RunResult) -> dict[str, Any]:
        """Record a failed run's error under its fingerprint (#356) and return what the failure
        events carry; on the first successful run after open errors, emit `run.resolved`."""
        from . import errors

        at = errors.now()
        if result.status == "failed" and result.error:
            step = self._failed_step
            cls, _ = errors.split(result.error)
            fp = errors.fingerprint(result.error, step)
            row = self.store.error_seen(self.spec.name, fp, step, cls, result.error, at)
            return {
                "fingerprint": fp,
                "error_count": row["count"],
                "first_seen": errors.iso(row["first_seen"]),
            }
        if result.status in ("succeeded", "no_change"):
            fixed = self.store.errors_resolve(self.spec.name, at)
            if fixed:
                self._emit(
                    run_id,
                    "run.resolved",
                    fingerprints=[r["fingerprint"] for r in fixed],
                    errors=len(fixed),
                    error_count=sum(int(r["count"]) for r in fixed),
                    first_seen=errors.iso(min(float(r["first_seen"]) for r in fixed)),
                    last_error=fixed[-1]["error"],
                )
        return {}

    def _classify(self, result: RunResult) -> None:
        """Set `result.outcome` from the status and the `outcomes` policy. Too many skipped runs in
        a row fail the run, so an unreachable source does not go unnoticed forever."""
        policy = self.spec.outcomes
        by_id = {s.id: s for s in self.spec.steps}
        if result.status == "failed":
            result.outcome = "failed"
        elif result.status == "no_change":  # a source raised NoChange
            result.outcome = "no_change"
        elif result.status == "skipped":
            result.outcome = "skipped"
            if policy.max_skipped is not None and self._skipped_streak() + 1 >= policy.max_skipped:
                result.status, result.outcome = "failed", "failed"
                result.error = f"{policy.max_skipped} skipped runs in a row; last: {result.reason}"
        elif (
            result.best_effort_failed
            or result.series_failed
            or any(by_id[s].fallback_outcome == "partial" for s in result.fallbacks if s in by_id)
            or (policy.on_rejected == "partial" and any(result.rejected.values()))
        ):
            result.outcome = "partial"
        elif (
            policy.on_empty == "no_change" and self._source_records and not any(self._source_records.values())
        ):
            result.outcome = "no_change"
        else:
            result.outcome = "success"

    def _skipped_streak(self) -> int:
        """Skipped runs of this pipeline right before the current one."""
        n = 0
        for run in self.store.recent_runs(self.spec.name, (self.spec.outcomes.max_skipped or 1) + 1):
            if run["id"] == self._run_id or run["status"] == "running":
                continue
            if run["status"] != "skipped":
                break
            n += 1
        return n

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

    def _seen_keys(self) -> registry.SeenKeys:
        """Keys `skip_seen` let through, kept per scope in the run store."""
        return registry.SeenKeys(
            self.spec.name,
            self.store.seen_filter,
            lambda scope, keys: self.store.seen_add(scope, keys, self._run_id or ""),
        )

    def _finish_after_run(self, run_id: str, result: RunResult) -> None:
        """Acknowledge (on success) or release (on failure) what queue consumers read. A failed
        acknowledgement fails the run: the messages will be delivered again."""
        hooks, self._after_run = self._after_run, []
        for on_success, on_failure in hooks:
            try:
                if result.status in ("succeeded", "no_change"):
                    on_success()
                elif on_failure is not None:
                    on_failure()
            except Exception as e:
                self._emit(run_id, "ack.failed", error=_describe(e))
                if result.status == "succeeded":
                    result.status = "failed"
                    result.error = f"acknowledging the messages read failed: {type(e).__name__}: {e}"


def parse_rows(text: str, truncated: bool = False) -> tuple[list[list], list[str], str]:
    """(rows, lines that are not JSON arrays, the text of the complete lines). When the answer
    was truncated, a last line that does not parse is the cut row: dropped, not rejected."""
    lines = [ln.strip() for ln in text.strip().strip("`").splitlines()]
    lines = [ln for ln in lines if ln and ln not in ("json", "```")]
    rows: list[list] = []
    bad: list[str] = []
    kept: list[str] = []
    for i, line in enumerate(lines):
        try:
            value = json.loads(line.rstrip(","))
        except ValueError:
            value = None
        if isinstance(value, list):
            rows.append(value)
            kept.append(line)
        elif not (truncated and i == len(lines) - 1):
            bad.append(line)
            kept.append(line)
    return rows, bad, "\n".join(kept)


def drop_overlap(got: list[list], new: list[list]) -> list[list]:
    """Rows a continuation repeats from the end of what came before (the longest such run)."""
    for k in range(min(len(got), len(new)), 0, -1):
        if got[-k:] == new[:k]:
            return new[k:]
    return new


def _apply_rules(
    step: StepSpec, records: Records, schema: type[BaseModel], base_dir: Any
) -> list[dict | None]:
    """The `rules:` of a hybrid agent step on each record, in order. Returns the `output:` fields
    for each record the rules handled, or None when a rule failed (an expression error, a value
    that is not of its semantic type, an exception in a fn) or an output field is missing, null
    or invalid. Those records go to the agent."""
    from .connectors.transforms import cast, compute

    tag = "\x00agentl_row"
    current: list[dict] = [{**r, tag: i} for i, r in enumerate(records)]
    for rule in step.rules:
        kind, body = next(iter(rule.items()))
        if kind == "fn":
            fn = registry.resolve_fn(body, base_dir)
            nxt = []
            for r in current:
                with contextlib.suppress(Exception):
                    out = fn({k: v for k, v in r.items() if k != tag})
                    if isinstance(out, dict):
                        nxt.append({**r, **out, tag: r[tag]})
            current = nxt
        elif kind == "compute":
            current = list(compute(current, fields=body))
        else:
            current = list(cast(current, fields=body))
    found: list[dict | None] = [None] * len(records)
    required = [f for f, t in (step.output or {}).items() if not t.endswith("?")]
    for r in current:
        if any(r.get(f) is None or r.get(f) == "" for f in required):
            continue
        try:
            value = schema.model_validate({f: r.get(f) for f in step.output or {}})
        except ValidationError:
            continue
        found[r[tag]] = value.model_dump()
    return found


def _route_instruction(question: str, labels: list[str]) -> str:
    return (
        f"{question}\n\nAnswer with the field `route` set to exactly one of these labels: "
        f"{', '.join(labels)}."
    )


def _review_instruction(review: str) -> str:
    return (
        f"{review}\n\nYou are checking data before it is delivered to the people who use it. The "
        "record holds a profile of today's data (row count, per-column stats, a few sample rows), "
        "the profile of the last delivered run (null on the first run) and the fixed checks that "
        "already failed. Set passed to false only for problems that should block delivery. Write "
        "each finding as one short sentence a business user understands, with the numbers."
    )


def _critic_instruction(criteria: str) -> str:
    return (
        "You review one answer extracted from a record. The input holds the record, the answer and "
        f"the criteria. Criteria: {criteria}\n\nSet approved to true only when the answer meets "
        "every criterion. When it does not, write feedback as short, concrete sentences saying "
        "which fields are wrong and why, so the answer can be fixed."
    )


def _judge_instruction(instruction: str) -> str:
    return (
        f"{instruction}\n\nSeveral reviewers answered this task for the same record. The input "
        "holds the record and their answers. Decide the correct answer from the record, using the "
        "answers as evidence, and return it in the requested shape."
    )


def _norm(v: Any) -> Any:
    """Jury votes compare strings after strip and casefold."""
    if isinstance(v, str):
        return v.strip().casefold()
    if isinstance(v, list):
        return [_norm(x) for x in v]
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    return v


def _regenerate_instruction(instruction: str, previous: dict, feedback: str) -> str:
    shown = json.dumps(previous, ensure_ascii=False, default=str)
    return (
        f"{instruction}\n\nA reviewer rejected your previous answer.\nPrevious answer: {shown}\n"
        f"Reviewer feedback: {feedback}\nAnswer again for the same record, fixing what the "
        "reviewer pointed out."
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
    hidden = {"_error", "_raw", "_dropped", "_value"}
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
    params: dict[str, Any] | None = None,
    db: str | os.PathLike | None = None,
    raise_on_failure: bool = False,
    **kwargs: Any,
) -> RunResult:
    """Run a pipeline from a spec or a YAML path. The stable entry point for schedulers
    and orchestrators (cron, Celery, Temporal, Airflow, Prefect, Lambda).

    `vars` override environment variables for `${VAR}` interpolation. `params` are the run
    parameters declared under `params:` (used as `${params.x}`); unknown, missing or mistyped
    values raise `SpecError` before anything runs. `db` is the run
    store path (default `.agentl/agentl.db`). With `raise_on_failure`, a failed run raises
    `PipelineFailed`, which is what most orchestrators need to trigger their retries."""
    if isinstance(pipeline, PipelineSpec):
        if params:
            raise ValueError("params= needs a pipeline path; a PipelineSpec is already resolved")
        spec = pipeline
    else:
        spec = load(Path(pipeline), {**os.environ, **(vars or {})}, params)
    if "store" in kwargs:
        result = Runner(spec, **kwargs).run()
    else:
        with open_store(db) as store:
            result = Runner(spec, store=store, **kwargs).run()
    if raise_on_failure and result.status == "failed":
        raise PipelineFailed(result)
    return result


def resume(
    pipeline: PipelineSpec | str | os.PathLike,
    run_id: str,
    *,
    vars: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    db: str | os.PathLike | None = None,
    raise_on_failure: bool = False,
    **kwargs: Any,
) -> RunResult:
    """Resume the failed run `run_id` (#339): completed steps are skipped, sinks included, and
    their outputs are read back from the checkpoints; the new run starts from the first failed or
    pending step. Raises `agentl.resume.ResumeRefused` when that is not safe (the spec changed, the
    run did not fail, or a completed writer would have to write again)."""
    return run(
        pipeline,
        vars=vars,
        params=params,
        db=db,
        raise_on_failure=raise_on_failure,
        resume_from=run_id,
        **kwargs,
    )


def replay_raw(
    pipeline: PipelineSpec | str | os.PathLike,
    run_id: str,
    *,
    vars: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    db: str | os.PathLike | None = None,
    raise_on_failure: bool = False,
    **kwargs: Any,
) -> RunResult:
    """Reprocess the `persist: raw` originals of run `run_id` with the current pipeline (#208):
    the raw steps and everything upstream of them are skipped, so no source is fetched. Raises
    `agentl.raw.ReplayRefused` when the run has no originals for every raw step."""
    return run(
        pipeline,
        vars=vars,
        params=params,
        db=db,
        raise_on_failure=raise_on_failure,
        raw_from=run_id,
        **kwargs,
    )
