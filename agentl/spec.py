"""Pipeline spec: YAML -> validated models."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .gaps import ExpectSpec

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
# ${params.name} (or ${param.name}): a run parameter declared under `params:`.
PARAM_REF = re.compile(r"\$\{params?\.([A-Za-z_][A-Za-z0-9_]*)\}")
PARAM_TYPES = ("str", "int", "float", "bool", "date")


class SpecError(ValueError):
    pass


class _Loader(yaml.SafeLoader):
    """YAML 1.2 booleans: only true/false. Keeps `on:`/`yes`/`no` as strings."""


_Loader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:bool"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for _first in "tTfF":
    _Loader.yaml_implicit_resolvers.setdefault(_first, []).append(
        ("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"))
    )


class PriceSpec(BaseModel):
    """USD per million tokens, from your provider's price page."""

    model_config = ConfigDict(extra="forbid")

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float | None = Field(default=None, ge=0)  # cached input; default: same as input
    cache_write: float | None = Field(default=None, ge=0)  # input written to the cache; default: input
    batch: float = Field(default=1.0, gt=0, le=1)  # multiplier for batch mode, e.g. 0.5; default: no discount


class LLMSpec(BaseModel):
    provider: str = "anthropic"
    model: str = "claude-opus-5-5"
    max_tokens: int = 4096
    options: dict[str, Any] = Field(default_factory=dict)
    price: PriceSpec | None = None
    # Other llms (by name) to try, in order, when a call to this one fails.
    fallback: list[str] = Field(default_factory=list)

    def cost(self, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
        """`input_tokens` is all input; `cache_read` and `cache_write` are the parts of it that hit
        or filled the provider's prompt cache, priced separately when the price says so."""
        p = self.price
        if p is None:
            return 0.0
        read = p.input if p.cache_read is None else p.cache_read
        write = p.input if p.cache_write is None else p.cache_write
        plain = max(0, input_tokens - cache_read - cache_write)
        usd = plain * p.input + cache_read * read + cache_write * write + output_tokens * p.output
        return usd / 1_000_000


class BudgetSpec(BaseModel):
    """Limits for one run. The run stops with `budget.exceeded` once a limit is reached."""

    model_config = ConfigDict(extra="forbid")

    max_tokens: int | None = Field(default=None, gt=0)
    max_usd: float | None = Field(default=None, gt=0)
    max_tool_calls: int | None = Field(default=None, gt=0)  # tool calls by agent steps, whole run


class MCPServerSpec(BaseModel):
    """An MCP server agent steps can use as tools: a local `command` (stdio) or a remote `url`
    (streamable HTTP)."""

    model_config = ConfigDict(extra="forbid")

    command: list[str] | None = None
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    timeout: float = Field(default=30, gt=0)

    @model_validator(mode="after")
    def _one_transport(self) -> MCPServerSpec:
        if bool(self.command) == bool(self.url):
            raise ValueError("an mcp server needs exactly one of 'command' or 'url'")
        return self


_EXPECTATIONS = (  # see checks.py
    "rows",
    "not_null",
    "unique",
    "accepted",
    "range",
    "fresh",
    "vs_last_run",
    "vs_runs",
    "rejected_rate",
    "pii",
)


class WaitSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: str
    condition: str = "rows > 0"  # rows OP N, AGG(field) OP value, or changed
    every: str | float = "15m"
    timeout: str | float = "6h"
    on_timeout: Literal["fail", "continue"] = "fail"

    @model_validator(mode="after")
    def _valid(self) -> WaitSpec:
        from .checks import parse_duration
        from .waits import parse_condition

        parse_condition(self.condition)
        if parse_duration(self.every) <= 0 or parse_duration(self.timeout) <= 0:
            raise ValueError("wait_until: every and timeout must be positive")
        return self


class ConcurrencySpec(BaseModel):
    """At most one run at a time per `key` (default: the pipeline name), across every worker that
    shares the run store. The lock is a lease renewed while the run lives; a dead worker's lease
    lapses after `lease`."""

    model_config = ConfigDict(extra="forbid")

    key: str = "{pipeline}"
    on_overlap: Literal["skip", "queue", "fail"] = "skip"
    lease: str | float = "5m"
    wait: str | float = "1h"  # queue: how long to wait for the other run before failing

    @model_validator(mode="after")
    def _valid(self) -> ConcurrencySpec:
        from .checks import parse_duration

        if parse_duration(self.lease) <= 0 or parse_duration(self.wait) <= 0:
            raise ValueError("concurrency: lease and wait must be positive")
        return self


class CursorSpec(BaseModel):
    """Incremental loading: the largest `field` seen in a successful run is kept in the run store
    and replaces `{cursor}` in the step's `with` on the next run (`initial` on the first)."""

    model_config = ConfigDict(extra="forbid")

    field: str
    initial: str | int | float | None = None


class WriterSpec(BaseModel):
    """Where a step's rejected records go: a writer connector and its settings."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    use: str
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")


RetryOn = Literal["transport", "http_5xx", "http_429", "any"]


def _retry_on_default() -> list[RetryOn]:
    return ["transport"]


# The last part of a connector name that marks a writer or a delivery (`postgres.write`, `slack.send`)
SINK_VERBS = ("write", "upload", "post", "produce", "publish", "send", "mark")


def is_sink(use: str | None) -> bool:
    """A connector that writes or delivers, judged by its name (`*.write`, `*.send`, ...)."""
    return bool(use) and (use or "").rsplit(".", 1)[-1] in SINK_VERBS


class RetrySpec(BaseModel):
    """Step retries (#296): up to `attempts` tries, waiting `backoff` doubled each time (with
    jitter) up to `max_backoff`, only for the errors in `on`."""

    model_config = ConfigDict(extra="forbid")

    attempts: int = Field(default=3, ge=1, le=20)
    backoff: str | float = "1s"
    max_backoff: str | float = "30s"
    on: list[RetryOn] = Field(default_factory=_retry_on_default, min_length=1)

    @model_validator(mode="after")
    def _durations(self) -> RetrySpec:
        from .checks import parse_duration

        if parse_duration(self.backoff) < 0 or parse_duration(self.max_backoff) < 0:
            raise ValueError("retries: backoff and max_backoff must not be negative")
        return self


class FallbackSpec(BaseModel):
    """One step body tried when the main body fails (#296): a connector and its settings."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    use: str
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")

    @model_validator(mode="before")
    @classmethod
    def _flat(cls, raw: Any) -> Any:
        if isinstance(raw, dict) and "fallback" in raw:
            raise ValueError("a fallback cannot have its own fallback; list every body in one list")
        if isinstance(raw, dict) and "use" not in raw:
            raise ValueError("each fallback is a step body {use, with}")
        return raw


class GoldenSpec(BaseModel):
    """Golden set of an agent step (#154): a JSONL file of records with the expected answer, for
    `agentl eval`. `min_accuracy` (share of records with every field right) makes it fail below."""

    model_config = ConfigDict(extra="forbid")

    file: str
    min_accuracy: float | None = Field(default=None, ge=0, le=1)
    tolerance: float = Field(default=0.0, ge=0)  # numbers within this absolute difference match

    @model_validator(mode="before")
    @classmethod
    def _short(cls, raw: Any) -> Any:
        return {"file": raw} if isinstance(raw, str) else raw


class ReviewSpec(BaseModel):
    """Human review of an agent step's answers (#153). An answer whose `confidence` is below
    `below`, or that matches the expression `when`, or every answer with `always`, goes to the
    review queue in the run store. With `hold` (the default) the record does not reach the next
    steps; with `hold: false` it goes on flagged `_review: pending`. An accepted or fixed answer is
    reused for the same input on later runs, with no llm call; a rejected one drops the record."""

    model_config = ConfigDict(extra="forbid")

    below: float | None = Field(default=None, ge=0, le=1)
    when: str | None = None
    always: bool = False
    hold: bool = True

    @field_validator("when")
    @classmethod
    def _when(cls, v: str | None) -> str | None:
        if v is not None:
            from . import expr

            try:
                expr.compile_expr(v)
            except expr.ExprError as e:
                raise ValueError(f"review when: {e}") from None
        return v

    @model_validator(mode="after")
    def _some(self) -> ReviewSpec:
        if self.below is None and self.when is None and not self.always:
            raise ValueError("review needs below: <confidence>, when: <expression> or always: true")
        return self


class RowRuleSpec(BaseModel):
    """A per-row rule of a check step (#218). `on_fail`: `drop` removes the row, `flag` keeps it
    and reports it, `fail` stops the step. An error inside the rule never stops the pipeline."""

    model_config = ConfigDict(extra="forbid")

    rule: Literal["not_null", "between", "accepted", "matches", "abs_diff_max"]
    column: str
    other: str | None = None  # abs_diff_max: the column compared with `column`
    min: float | None = None
    max: float | None = None
    values: list[Any] | None = None
    pattern: str | None = None
    on_fail: Literal["drop", "flag", "fail"] = "flag"

    @model_validator(mode="after")
    def _needs(self) -> RowRuleSpec:
        need = {
            "between": self.min is not None or self.max is not None,
            "accepted": self.values is not None,
            "matches": self.pattern is not None,
            "abs_diff_max": self.other is not None and self.max is not None,
        }.get(self.rule, True)
        if not need:
            hint = {"between": "min and/or max", "accepted": "values", "matches": "pattern"}
            raise ValueError(f"rule {self.rule}: needs {hint.get(self.rule, 'other and max')}")
        return self


def _row_rule(step: str, raw: Any) -> RowRuleSpec:
    if (
        isinstance(raw, dict)
        and "rule" not in raw
        and len(raw) == 1
        and next(iter(raw))
        in (
            "compute",
            "cast",
            "fn",
        )
    ):
        raise ValueError(
            f"step '{step}': {next(iter(raw))}: is a hybrid rule of agent steps; a check step's "
            "rules are per-row checks (rule: not_null | between | accepted | matches | abs_diff_max)"
        )
    return RowRuleSpec.model_validate(raw)


class AttemptSpec(BaseModel):
    """One try of an agent step at a record (#198). `attempts:` lists them in order; a record moves
    to the next attempt when its answer fails validation."""

    model_config = ConfigDict(extra="forbid")

    llm: str | None = None  # default: the step's llm
    # strict: any invalid field fails the attempt. lenient: invalid list items and optional fields
    # are dropped (each one quarantined with its reason) and the record goes on.
    validate_: Literal["strict", "lenient"] = Field(default="strict", alias="validate")
    # Add the previous attempt's validation errors (field, rule, value) and a stricter reminder.
    feedback: bool = False
    on_invalid: Literal["quarantine"] = "quarantine"


class CriticSpec(BaseModel):
    """A second llm reviews each answer of an agent step (#299). A rejected answer goes back to the
    step's llm with the reviewer's feedback, up to `max_rounds` rejections; then the record is
    quarantined."""

    model_config = ConfigDict(extra="forbid")

    llm: str | None = None  # default: the step's llm
    criteria: str
    max_rounds: int = Field(default=2, ge=1, le=5)

    @field_validator("criteria")
    @classmethod
    def _criteria(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("critic criteria must not be empty")
        return v


class JurySpec(BaseModel):
    """Several llms answer each record independently and a decision picks the answer (#299):
    `majority` (most jurors agree on `fields`, a tie quarantines the record) or `judge` (a judge
    llm reads the jurors' answers and writes the final one)."""

    model_config = ConfigDict(extra="forbid")

    llms: list[str] = Field(min_length=2)
    decide: Literal["majority", "judge"] = "majority"
    judge: str | None = None
    # low_confidence: only records whose first answer has `confidence` below `escalate_below`.
    when: Literal["always", "low_confidence"] = "low_confidence"
    fields: list[str] = Field(default_factory=list)  # default: every output field

    @model_validator(mode="after")
    def _judge(self) -> JurySpec:
        if len(set(self.llms)) != len(self.llms):
            raise ValueError("jury llms must be different entries")
        if self.decide == "judge" and not self.judge:
            raise ValueError("jury decide: judge needs judge: <llm name>")
        if self.decide != "judge" and self.judge:
            raise ValueError("jury judge: is only used with decide: judge")
        return self


DROP = "drop"  # reserved route target: the record is discarded
ROUTE_OUTPUT = {"route": "str"}  # the implicit output of a router


class StepSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str
    use: str | None = None
    fn: str | None = None
    agent: str | None = None
    # Data check: the step to check (its input); expectations, an optional agent review, and what
    # to do when it fails. The records pass through unchanged.
    check: str | None = None
    expect: dict[str, Any] = Field(default_factory=dict)
    # On a check step: the instruction of the agent review of the data. On an agent step: the
    # human review queue (#153), see ReviewSpec.
    review: str | ReviewSpec | None = None
    on_fail: Literal["stop", "warn"] = "stop"
    # Layout drift (#178): compare the input's columns and types with the last run's. See drift.py.
    on_drift: Literal["warn", "fail", "remap"] | None = None
    # `rules:` depends on the step kind (validated in _one_kind):
    # - on a `check` step (#218): per-row rules (RowRuleSpec), with `identity` naming the columns
    #   that identify a finding from one day to the next (default: the rule's column value);
    # - on an `agent` step (#124, #195, hybrid): rules tried in order on each record before the
    #   agent; a record whose rules all succeed and give valid `output:` fields never reaches the
    #   llm. Items are one-key dicts: {compute: {field: expr}}, {cast: {field: type}} or
    #   {fn: "module:function"}.
    rules: list[Any] = Field(default_factory=list)
    identity: list[str] = Field(default_factory=list)
    # Per rule (#185): fail (default), warn (event only) or reject (drop the failing rows).
    severity: dict[str, Literal["fail", "warn", "reject"]] = Field(default_factory=dict)
    # Wait for a source with no fixed publishing time: re-run `step` every `every` until
    # `condition` holds, for at most `timeout`. See WaitSpec.
    wait_until: WaitSpec | None = None
    cursor: CursorSpec | None = None
    input: str | list[str] | None = None
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    output: dict[str, str] | None = None
    llm: str = "default"
    # Cascade: `llm: [cheap, strong]` puts the rest here. A record whose answer stays invalid after
    # the retry, or whose `confidence` is below `escalate_below`, goes to the next llm.
    escalate: list[str] = Field(default_factory=list)
    escalate_below: float | None = Field(default=None, ge=0, le=1)
    # Attempts with validator feedback and progressive strictness (#198). Replaces the default
    # "two tries per llm" when set. See AttemptSpec.
    attempts: list[AttemptSpec] = Field(default_factory=list)
    # A reviewer llm approves each answer or sends it back with feedback (#299). See CriticSpec.
    critic: CriticSpec | None = None
    # Several llms answer and a vote or a judge decides (#299). See JurySpec.
    jury: JurySpec | None = None
    jury_tag: bool = False  # add `_agreement` (share of jurors behind the answer) to each record
    # Router (#299): an agent step that labels each record with one of these keys and sends it
    # only to the mapped step (`drop` discards it). Each target reads the router as its input and
    # receives just its branch. `rules:` items here are {when: expr, route: label}.
    routes: dict[str, str] | None = None
    route_tag: bool = False  # add `_route` (the label) to each routed record
    concurrency: int = 4
    records_per_call: int = Field(default=1, ge=1, le=200)
    # Compact rows (#215): the model answers one JSON array per line in the column order of
    # `output`, and agentl builds the records. One input record can give many rows.
    output_mode: Literal["records", "rows"] = "records"
    on_truncated: Literal["continue", "fail"] = "continue"  # rows mode: answer cut by max_tokens
    max_continuations: int = Field(default=4, ge=0, le=50)
    mode: Literal["sync", "batch"] = "sync"
    batch_poll_seconds: float = Field(default=30, gt=0)
    batch_timeout_seconds: float = Field(default=86400, gt=0)
    cache: bool | None = None  # default: True for agent steps
    cache_ttl: str | float | None = None  # cached results expire after this long; default: never
    # Part of the cache key: change it to stop reusing results after editing a `fn` or transform.
    cache_version: str | int | None = None
    # Tools the agent may call, per record: "http.get", "sqlite.query", "fn:module:function",
    # "mcp:server" or "mcp:server/tool"; a one-key dict adds settings, e.g. {sqlite.query: {db: x}}.
    tools: list[str | dict[str, dict[str, Any]]] = Field(default_factory=list)
    max_tool_calls: int = Field(default=8, ge=1, le=100)  # per record
    write_tools: list[str] = Field(default_factory=list)  # tools allowed even though they can write
    # Contract (#122): checks every record the step outputs (after `output:` on agent steps)
    # against `cast` types. See agentl/contracts.py.
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    on_bad: Literal["reject", "fail", "coerce"] = "reject"
    extra: Literal["keep", "drop", "forbid"] = "keep"
    rejected_to: WriterSpec | None = None  # writes the step's rejected records (any reason)
    golden: GoldenSpec | None = None  # expected answers for `agentl eval` (agent steps)
    # Criticality (#206): `required: false` (or `criticality: best_effort`) makes a failure of this
    # step a recorded warning instead of a failed run. No step may read its output.
    required: bool = True
    # Order-only dependencies: steps that must finish (succeed, or fail best effort) first.
    after: list[str] = Field(default_factory=list)
    # Retries and fallbacks (#296): retry the step on matching errors, then try each fallback body
    # ({use, with}) in order with the same input. See RetrySpec and FallbackSpec.
    retries: RetrySpec | None = None
    fallback: list[FallbackSpec] = Field(default_factory=list)
    fallback_tag: bool = False  # add `_source` ("main" or "fallback[i]") to each record
    fallback_outcome: Literal["success", "partial"] = "partial"  # run outcome when a fallback ran
    fallback_writer: bool = False  # allow a fallback on a writer (it may write twice)

    def _check_jury(self, jury: JurySpec) -> None:
        sid = self.id
        if not self.agent:
            raise ValueError(f"step '{sid}': only agent steps take a jury")
        if self.output_mode == "rows":
            raise ValueError(f"step '{sid}': jury is not supported with output_mode rows")
        if self.mode == "batch" or self.records_per_call > 1:
            raise ValueError(
                f"step '{sid}': jury works one record per call; drop mode batch and records_per_call"
            )
        unknown = [f for f in jury.fields if f not in (self.output or {})]
        if unknown:
            raise ValueError(f"step '{sid}': jury fields not in output: {', '.join(unknown)}")
        if jury.when == "low_confidence" and (
            "confidence" not in (self.output or {}) or self.escalate_below is None
        ):
            raise ValueError(
                f"step '{sid}': jury when: low_confidence needs a confidence output field and"
                " escalate_below; use when: always otherwise"
            )

    def _check_review(self, review: ReviewSpec) -> None:
        sid = self.id
        if not self.agent:
            raise ValueError(f"step '{sid}': only agent steps take a review mapping; a check takes text")
        if self.routes is not None or self.output_mode == "rows":
            raise ValueError(f"step '{sid}': review does not work on routers or with output_mode rows")
        if review.below is not None and (self.output or {}).get("confidence", "").rstrip("?") not in (
            "float",
            "int",
        ):
            raise ValueError(f"step '{sid}': review below needs a 'confidence: float' output field")

    @property
    def llms(self) -> list[str]:
        """Every llm the step may call, in order, without repeats."""
        names = [self.llm, *self.escalate, *(a.llm or self.llm for a in self.attempts)]
        if self.critic is not None:
            names.append(self.critic.llm or self.llm)
        if self.jury is not None:
            names.extend(self.jury.llms)
            if self.jury.judge:
                names.append(self.jury.judge)
        return list(dict.fromkeys(names))

    @model_validator(mode="before")
    @classmethod
    def _cascade(cls, raw: Any) -> Any:
        if isinstance(raw, dict) and isinstance(raw.get("check"), dict):
            # `check: {schema_matches: sheet, on_drift: fail}` is short for check + on_drift
            short = dict(raw["check"])
            if set(short) - {"schema_matches", "on_drift"} or "schema_matches" not in short:
                raise ValueError(
                    f"step '{raw.get('id')}': check as a mapping takes schema_matches and on_drift"
                )
            if raw.get("on_drift") is not None and "on_drift" in short:
                raise ValueError(f"step '{raw.get('id')}': on_drift is set twice")
            raw = {**raw, "check": short["schema_matches"], "on_drift": short.get("on_drift", "warn")}
        if isinstance(raw, dict) and "sql" in raw:  # `sql: select ...` is short for `use: sql`
            if raw.get("use") not in (None, "sql"):
                raise ValueError(f"step '{raw.get('id')}': sql is a step kind; drop 'use'")
            query = raw["sql"]
            rest = {k: v for k, v in raw.items() if k != "sql"}
            raw = {**rest, "use": "sql", "with": {**(rest.get("with") or {}), "query": query}}
        if isinstance(raw, dict) and isinstance(raw.get("llm"), list):
            names = [str(n) for n in raw["llm"]]
            if not names:
                raise ValueError(f"step '{raw.get('id')}': llm list is empty")
            raw = {**raw, "llm": names[0], "escalate": names[1:]}
        if isinstance(raw, dict) and "criticality" in raw:
            rest = {k: v for k, v in raw.items() if k != "criticality"}
            value = raw["criticality"]
            if value not in ("required", "best_effort"):
                raise ValueError(f"step '{raw.get('id')}': criticality is 'required' or 'best_effort'")
            if "required" in rest and rest["required"] != (value == "required"):
                raise ValueError(f"step '{raw.get('id')}': set either required or criticality")
            raw = {**rest, "required": value == "required"}
        if isinstance(raw, dict) and isinstance(raw.get("after"), str):
            raw = {**raw, "after": [raw["after"]]}
        if isinstance(raw, dict) and raw.get("routes") is not None:
            # A router answers one label per record; its output schema is implicit.
            if raw.get("output") not in (None, ROUTE_OUTPUT):
                raise ValueError(
                    f"step '{raw.get('id')}': a router has no 'output'; it answers a route label"
                )
            raw = {**raw, "output": dict(ROUTE_OUTPUT)}
        return raw

    @model_validator(mode="after")
    def _one_kind(self) -> StepSpec:
        kinds = [k for k in ("use", "fn", "agent", "check", "wait_until") if getattr(self, k)]
        if len(kinds) != 1:
            raise ValueError(f"step '{self.id}': set exactly one of use/fn/agent/check/wait_until")
        if self.wait_until and self.input is not None:
            raise ValueError(f"step '{self.id}': wait_until names its source in 'step'; drop 'input'")
        if self.check and self.input not in (None, self.check):
            raise ValueError(f"step '{self.id}': check names its input; drop 'input'")
        if self.check and not (self.expect or self.review or self.rules or self.on_drift):
            raise ValueError(f"step '{self.id}': a check needs 'expect', 'review', 'rules' or 'on_drift'")
        if self.identity and not self.check:
            raise ValueError(f"step '{self.id}': identity belongs to check steps (with per-row rules)")
        if self.rules and not (self.check or self.agent):
            raise ValueError(
                f"step '{self.id}': rules go on an agent step (hybrid: rules first, the llm for the "
                "rows they miss) or on a check step (per-row check rules)"
            )
        if self.check:
            self.rules = [r if isinstance(r, RowRuleSpec) else _row_rule(self.id, r) for r in self.rules]
        if self.on_drift and not self.check:
            raise ValueError(f"step '{self.id}': on_drift belongs to check steps")
        unknown = [k for k in self.expect if k not in _EXPECTATIONS]
        if unknown:
            raise ValueError(
                f"step '{self.id}': unknown expectation {unknown}. Use {', '.join(_EXPECTATIONS)}"
            )
        if self.severity:
            from .checks import ROW_RULES

            stray = [k for k in self.severity if k not in self.expect and k != "review"]
            if stray:
                raise ValueError(f"step '{self.id}': severity for {stray}, which is not in 'expect'")
            wrong = [k for k, v in self.severity.items() if v == "reject" and k not in ROW_RULES]
            if wrong:
                raise ValueError(
                    f"step '{self.id}': severity reject drops rows, so it works only for "
                    f"{', '.join(ROW_RULES)} (not {wrong})"
                )
        if "vs_runs" in self.expect:
            vr = self.expect["vs_runs"]
            if (
                not isinstance(vr, dict)
                or not isinstance(vr.get("last", 5), int)
                or not 1 <= vr.get("last", 5) <= 50
            ):
                raise ValueError(f"step '{self.id}': vs_runs needs a mapping like {{last: 5, rows: 30%}}")
            unknown_vr = [k for k in vr if k not in ("last", "rows", "sum")]
            if unknown_vr:
                raise ValueError(f"step '{self.id}': vs_runs takes last, rows and sum, not {unknown_vr}")
        if (
            self.expect or (isinstance(self.review, str) and not self.agent) or self.severity
        ) and not self.check:
            raise ValueError(f"step '{self.id}': expect and review belong to check steps")
        if self.agent and not self.output:
            raise ValueError(f"step '{self.id}': agent steps need an 'output' schema")
        if self.mode == "batch" and self.records_per_call > 1:
            raise ValueError(
                f"step '{self.id}': mode batch sends one record per request; drop records_per_call"
            )
        if self.output_mode == "rows":
            if not self.agent:
                raise ValueError(f"step '{self.id}': output_mode rows belongs to agent steps")
            if self.mode == "batch" or self.records_per_call > 1 or self.tools or self.escalate or self.rules:
                raise ValueError(
                    f"step '{self.id}': output_mode rows works one record per call, without "
                    "mode batch, records_per_call, tools, a list of llms or rules:"
                )
            if self.attempts:
                raise ValueError(
                    f"step '{self.id}': output_mode rows has its own retries (continuations, a bad "
                    "row rejected on its own); attempts: works with whole-record answers only"
                )

        if self.routes is not None:
            self._check_router()
        for rule in self.rules if self.agent and self.routes is None else []:
            if not isinstance(rule, dict):
                raise ValueError(
                    f"step '{self.id}': a rule is compute: {{...}}, cast: {{...}} or fn: mod:func"
                )
            if len(rule) != 1 or next(iter(rule)) not in ("compute", "cast", "fn"):
                raise ValueError(
                    f"step '{self.id}': a rule is compute: {{...}}, cast: {{...}} or fn: mod:func"
                )
            kind, body = next(iter(rule.items()))
            if kind == "fn" and not (isinstance(body, str) and ":" in body):
                raise ValueError(f"step '{self.id}': rule fn must look like 'module:function'")
            if kind != "fn" and not (isinstance(body, dict) and body):
                raise ValueError(f"step '{self.id}': rule {kind} maps fields to expressions or types")
            if kind == "compute":
                from . import expr

                for text in body.values():
                    try:
                        expr.compile_expr(str(text))
                    except expr.ExprError as e:
                        raise ValueError(f"step '{self.id}': rule compute: {e}") from None
        if self.tools and not self.agent:
            raise ValueError(f"step '{self.id}': only agent steps take tools")
        if self.golden and not self.agent:
            raise ValueError(f"step '{self.id}': only agent steps take a golden set")
        if isinstance(self.review, ReviewSpec):
            self._check_review(self.review)
        elif self.review is not None and self.agent:
            raise ValueError(
                f"step '{self.id}': review on an agent step is a mapping, e.g. review: {{below: 0.8}}"
            )
        if self.tools and (self.mode == "batch" or self.records_per_call > 1):
            raise ValueError(
                f"step '{self.id}': tools work one record per call; drop mode batch and records_per_call"
            )
        for t in self.tools:
            if isinstance(t, dict) and len(t) != 1:
                raise ValueError(f"step '{self.id}': write each tool with settings as its own item")
        if self.cursor and not (self.use or self.fn):
            raise ValueError(f"step '{self.id}': cursor goes on a use or fn step that reads the data")
        if self.critic is not None:
            if not self.agent:
                raise ValueError(f"step '{self.id}': only agent steps take a critic")
            if self.output_mode == "rows":
                raise ValueError(
                    f"step '{self.id}': critic is not supported with output_mode rows; it reviews"
                    " one answer per record"
                )
        if self.jury is not None:
            self._check_jury(self.jury)
        elif self.jury_tag:
            raise ValueError(f"step '{self.id}': jury_tag needs jury")
        if self.escalate and not self.agent:
            raise ValueError(f"step '{self.id}': only agent steps take a list of llms")
        if self.escalate and self.mode == "batch":
            raise ValueError(f"step '{self.id}': a cascade of llms does not work with mode batch")
        if self.attempts:
            if not self.agent:
                raise ValueError(f"step '{self.id}': only agent steps take attempts")
            if self.escalate:
                raise ValueError(f"step '{self.id}': set the llm of each attempt instead of a list of llms")
            if self.mode == "batch" or self.records_per_call > 1:
                raise ValueError(
                    f"step '{self.id}': attempts work one record per call; drop mode batch and"
                    " records_per_call"
                )
            if any(a.validate_ == "lenient" for a in self.attempts[:-1]):
                raise ValueError(f"step '{self.id}': only the last attempt can be lenient")
        if self.escalate_below is not None:
            if not self.escalate and self.jury is None:
                raise ValueError(
                    f"step '{self.id}': escalate_below needs a list of llms or a jury,"
                    " e.g. llm: [cheap, strong]"
                )
            if (self.output or {}).get("confidence", "").rstrip("?") not in ("float", "int"):
                raise ValueError(f"step '{self.id}': escalate_below needs a 'confidence: float' output field")
        if self.schema_ is not None:
            from .contracts import parse as parse_contract

            try:
                parse_contract(self.schema_)
            except ValueError as e:
                raise ValueError(f"step '{self.id}': {e}") from None
        else:
            for name, default in (("on_bad", "reject"), ("extra", "keep")):
                if getattr(self, name) != default:
                    raise ValueError(f"step '{self.id}': {name} goes with a 'schema'")
        if "rejected_rate" in self.expect:
            from .checks import rejected_limits

            rejected_limits(self.expect["rejected_rate"], self.check or "")
        if self.records_per_call > 1 and "index" in (self.output or {}):
            raise ValueError(f"step '{self.id}': with records_per_call, 'index' is reserved in output")
        if self.retries and (self.check or self.wait_until):
            raise ValueError(f"step '{self.id}': retries go on use, fn or agent steps")
        if self.fallback:
            if self.agent:
                raise ValueError(
                    f"step '{self.id}': agent steps fall back between llms; set `fallback:` on the "
                    "llm instead (llm: {main: {..., fallback: [backup]}}) or use llm: [cheap, strong]"
                )
            if not (self.use or self.fn):
                raise ValueError(f"step '{self.id}': fallback goes on a use or fn step")
            sinks = [u for u in (self.use, *(f.use for f in self.fallback)) if is_sink(u)]
            if sinks and not self.fallback_writer:
                raise ValueError(
                    f"step '{self.id}': fallback on a writer ({sinks[0]}) may write twice; set "
                    "`fallback_writer: true` if that is safe"
                )
        elif self.fallback_tag or self.fallback_writer or self.fallback_outcome != "partial":
            raise ValueError(
                f"step '{self.id}': fallback_tag, fallback_outcome and fallback_writer need a fallback"
            )
        if self.route_tag and self.routes is None:
            raise ValueError(f"step '{self.id}': route_tag goes with routes")
        return self

    def _check_router(self) -> None:
        routes = self.routes or {}
        if not self.agent:
            raise ValueError(f"step '{self.id}': routes go on an agent step (the router's question)")
        if not routes:
            raise ValueError(f"step '{self.id}': routes is empty; map each label to a step id or drop")
        for label, target in routes.items():
            if not label.isidentifier():
                raise ValueError(f"step '{self.id}': route label '{label}' must be an identifier")
            if not isinstance(target, str) or not target:
                raise ValueError(f"step '{self.id}': route '{label}' needs a target step id or drop")
            if target == self.id:
                raise ValueError(f"step '{self.id}': route '{label}' points at the router itself")
        if self.critic is not None or self.output_mode == "rows":
            raise ValueError(f"step '{self.id}': a router takes no critic and no output_mode rows")
        from . import expr

        for rule in self.rules:
            if not (isinstance(rule, dict) and set(rule) == {"when", "route"}):
                raise ValueError(f"step '{self.id}': a router rule is {{when: <expression>, route: <label>}}")
            if rule["route"] not in routes:
                raise ValueError(
                    f"step '{self.id}': rule route '{rule['route']}' is not a label in routes "
                    f"({', '.join(routes)})"
                )
            try:
                expr.compile_expr(str(rule["when"]))
            except expr.ExprError as e:
                raise ValueError(f"step '{self.id}': rule when: {e}") from None

    @property
    def targets(self) -> dict[str, list[str]]:
        """A router's targets: step id -> the labels it receives (`drop` left out)."""
        out: dict[str, list[str]] = {}
        for label, target in (self.routes or {}).items():
            if target != DROP:
                out.setdefault(target, []).append(label)
        return out

    @property
    def kind(self) -> str:
        if self.wait_until:
            return "wait"
        return "use" if self.use else "fn" if self.fn else "check" if self.check else "agent"

    @property
    def inputs(self) -> list[str]:
        if self.input is None:
            if self.wait_until:
                return [self.wait_until.step]
            return [self.check] if self.check else []
        return [self.input] if isinstance(self.input, str) else list(self.input)

    @property
    def depends(self) -> list[str]:
        """Steps that must finish before this one: its inputs, then its `after` list."""
        return [*self.inputs, *(a for a in self.after if a not in self.inputs)]

    @property
    def cached(self) -> bool:
        return self.kind == "agent" if self.cache is None else self.cache


class Channel(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str | None = None  # defaults to the channel name


class Rule(BaseModel):
    on: str | list[str]
    to: list[str]
    when: str | None = None
    summary: str | None = None  # "agent" -> LLM summary of the run
    # `on: records`: send the records `step` produced, only when it produced any
    step: str | None = None
    template: str | None = None  # one line per record, e.g. "{id}: {message}"; default JSON
    max_rows: int = Field(default=20, ge=1)

    @model_validator(mode="after")
    def _records_rule(self) -> Rule:
        kinds = [self.on] if isinstance(self.on, str) else self.on
        if "records" in kinds and not self.step:
            raise ValueError("notify rule 'on: records' needs 'step: <step id>'")
        if self.step and "records" not in kinds:
            raise ValueError("notify rule 'step:' is only used with 'on: records'")
        return self


class NotifySpec(BaseModel):
    channels: dict[str, Channel] = Field(default_factory=dict)
    rules: list[Rule] = Field(default_factory=list)


class PublicationSpec(BaseModel):
    """When a publication is due: weekdays and a local time, with a holiday calendar."""

    model_config = ConfigDict(extra="forbid")

    days: list[str] = Field(default_factory=list)  # mon..sun
    at: str = "00:00"  # "16:00", in the schedule's tz
    calendar: str | None = None  # us_federal, br_national
    exceptions: str | None = None  # file with one extra holiday (YYYY-MM-DD) per line
    if_holiday: Literal["skip", "next_business_day", "previous_business_day", "run"] = "skip"

    @model_validator(mode="after")
    def _check(self) -> PublicationSpec:
        import datetime as _dt

        from .schedule import _DAYS, CALENDARS

        self.days = [d.lower() for d in self.days]
        bad = [d for d in self.days if d not in _DAYS]
        if bad:
            raise ValueError(f"expect.days: unknown day(s) {bad}; use mon, tue, wed, thu, fri, sat, sun")
        try:
            _dt.time.fromisoformat(self.at)
        except ValueError as e:
            raise ValueError(f"expect.at '{self.at}': use HH:MM") from e
        if self.calendar and self.calendar not in CALENDARS:
            raise ValueError(f"expect.calendar '{self.calendar}': use one of {sorted(CALENDARS)}")
        return self


class PollStage(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    for_: str = Field(alias="for")  # how long this stage lasts: "15m"
    every: str  # how often it fires: "1m"

    @model_validator(mode="after")
    def _check(self) -> PollStage:
        from .checks import parse_duration

        if parse_duration(self.every) <= 0 or parse_duration(self.for_) <= 0:
            raise ValueError("poll stages need positive `for` and `every`")
        return self


class PollSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cadence: list[PollStage] = Field(default_factory=list)
    stop_when: Literal["success_in_cycle"] | None = None


class ActiveSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    between: tuple[date, date]


class ScheduleSpec(BaseModel):
    """A schedule by publication cycle: expected days, holidays, staged polling."""

    model_config = ConfigDict(extra="forbid")

    tz: str  # required: "America/New_York"
    expect: PublicationSpec | None = None
    poll: PollSpec | None = None
    active: ActiveSpec | None = None
    extra_dates: list[date] = Field(default_factory=list)  # cycles on top of `expect`
    skip_dates: list[date] = Field(default_factory=list)  # expected days with no cycle

    @model_validator(mode="after")
    def _check(self) -> ScheduleSpec:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(self.tz)
        except Exception as e:
            raise ValueError(f"schedule.tz '{self.tz}': not a known time zone (e.g. America/New_York)") from e
        if not (self.expect and self.expect.days) and not self.extra_dates:
            raise ValueError("a schedule needs `expect.days` or `extra_dates`")
        return self


class TriggerSpec(BaseModel):
    """What starts a run under `agentl serve`: a cron schedule, a webhook path or new files."""

    model_config = ConfigDict(extra="forbid")

    cron: str | None = None  # "0 7 * * 1-5", local time unless `timezone`
    timezone: str | None = None
    schedule: ScheduleSpec | None = None  # publication cycles; see agentl/schedule.py
    webhook: str | None = None  # "/hooks/invoices"
    secret: str | None = None  # webhook: Bearer token or HMAC-SHA256 signature key
    files: str | None = None  # glob, local or remote: "inbox/*.csv", "s3://bucket/in/*.xlsx"
    every: str | float = "1m"  # files: how often to look; alone: run again at this interval
    storage_options: dict[str, Any] = Field(default_factory=dict)
    after: str | None = None  # another pipeline's name: run when that one finishes
    status: Literal["succeeded", "failed", "any"] = "succeeded"  # after: which outcomes count

    @model_validator(mode="after")
    def _one(self) -> TriggerSpec:
        kinds = [k for k in ("cron", "schedule", "webhook", "files", "after") if getattr(self, k)]
        if not kinds and "every" in self.model_fields_set:
            kinds = ["every"]  # on its own: run again every `every`, never two at once
        if len(kinds) != 1:
            raise ValueError("a trigger has exactly one of cron, schedule, webhook, files, after or every")
        if self.cron:
            from .triggers import Cron

            Cron(self.cron)
        if self.webhook and not self.webhook.startswith("/"):
            raise ValueError(f"webhook path must start with '/': {self.webhook}")
        if self.secret and not self.webhook:
            raise ValueError("secret belongs to webhook triggers")
        if "status" in self.model_fields_set and not self.after:
            raise ValueError("status belongs to after triggers")
        return self

    @property
    def kind(self) -> str:
        for k in ("cron", "schedule", "webhook", "files", "after"):
            if getattr(self, k):
                return k
        return "every"


class EmitSpec(BaseModel):
    """POST an event to a webhook: `{id, topic, pipeline, run_id, status, payload}`."""

    model_config = ConfigDict(extra="forbid")

    url: str
    topic: str
    payload: dict[str, Any] = Field(default_factory=dict)
    secret: str | None = None  # signs the body: X-Signature-256: sha256=<hmac>


class ActionSpec(BaseModel):
    """One `on_success` / `on_failure` / `after` action under `agentl serve`."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    trigger: str | None = None  # another pipeline to run, its `use: trigger` step gets `with`
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    emit: EmitSpec | None = None
    when: str | None = None  # e.g. "all_ok and rows_changed > 0"

    @model_validator(mode="after")
    def _one(self) -> ActionSpec:
        if bool(self.trigger) == bool(self.emit):
            raise ValueError("an action has exactly one of trigger or emit")
        if self.when:
            from .chain import check_when

            check_when(self.when)
        return self


class LineageSpec(BaseModel):
    """`lineage:` in the YAML. `url` is the OpenLineage HTTP endpoint base (e.g. Marquez at
    http://localhost:5000); events go to `<url>/api/v1/lineage`. `file` appends one JSON event per
    line. `api_key` is sent as a Bearer token; use `${VAR}` for it."""

    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    file: str | None = None
    namespace: str = "agentl"
    api_key: str | None = None
    endpoint: str = "api/v1/lineage"


Outcome = Literal["success", "no_change", "skipped", "partial", "failed"]
OUTCOMES: tuple[str, ...] = ("success", "no_change", "skipped", "partial", "failed")


class OutcomesSpec(BaseModel):
    """How a finished run is classified (#188). `status` stays `succeeded`/`failed` for
    every outcome but `skipped`, whose status is `skipped`."""

    model_config = ConfigDict(extra="forbid")
    # Timeouts and connection errors only; HTTP 4xx/5xx and parsing errors stay failed.
    on_transport_error: Literal["failed", "skipped_upstream"] = "failed"
    on_empty: Literal["no_change", "success"] = "no_change"  # every source step read 0 records
    on_rejected: Literal["partial", "success"] = "partial"  # the run quarantined records
    alert_on: list[Outcome] = ["failed", "partial"]  # pydantic copies mutable defaults
    # This many skipped runs in a row (this one included) turn the run into failed.
    max_skipped: int | None = Field(default=3, ge=1)
    exit_codes: dict[Outcome, int] = Field(default_factory=dict)

    def exit_code(self, outcome: str) -> int:
        default = {"failed": 1, "partial": 0}.get(outcome, 0)
        return self.exit_codes.get(outcome, default)  # type: ignore[call-overload]


class ReportSpec(BaseModel):
    """What a failed run does when its error looks like an agentl bug (#286). `prompt` prints the
    `agentl report` hint, `open` also opens the prefilled issue form in the browser (nothing is
    submitted), `off` prints nothing."""

    model_config = ConfigDict(extra="forbid")
    on_bug: Literal["prompt", "open", "off"] = "prompt"


class WindowSpec(BaseModel):
    """Fetch windows: `${window.start}` / `${window.end}` (ISO dates, end exclusive) in a step's
    `with:`. A run re-reads the last `lookback`; `agentl backfill` covers any range. Either way the
    range is split into chunks of at most `max_span`, cut at `align` boundaries."""

    model_config = ConfigDict(extra="forbid")

    lookback: str = "1d"
    max_span: str | None = None
    align: Literal["none", "calendar_month", "week"] = "none"
    order: Literal["oldest_first", "newest_first"] = "oldest_first"

    @field_validator("lookback", "max_span")
    @classmethod
    def _duration(cls, v: str | None) -> str | None:
        if v is not None:
            from .windows import days

            days(v)
        return v


class ParamSpec(BaseModel):
    """A run parameter: `agentl run --param name=value`, `agentl.run(..., params={...})`."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["str", "int", "float", "bool", "date"] = "str"
    default: Any = None
    required: bool | None = None  # default: required when there is no default
    help: str | None = None

    @model_validator(mode="after")
    def _default_type(self) -> ParamSpec:
        if self.default is not None:
            self.default = coerce_param("default", self.type, self.default)
        return self

    @property
    def is_required(self) -> bool:
        return self.default is None if self.required is None else self.required


def coerce_param(name: str, type_: str, value: Any) -> Any:
    """Convert a CLI string (or a Python value) to the declared type; raise SpecError if it is not one."""
    import datetime as _dt

    try:
        if type_ == "str":
            if isinstance(value, (dict, list)):
                raise ValueError
            return str(value)
        if type_ == "bool":
            if isinstance(value, bool):
                return value
            low = str(value).strip().lower()
            if low in ("true", "1", "yes"):
                return True
            if low in ("false", "0", "no"):
                return False
            raise ValueError
        if type_ == "int":
            if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
                raise ValueError
            return int(value)
        if type_ == "float":
            if isinstance(value, bool):
                raise ValueError
            return float(value)
        if type_ == "date":
            if isinstance(value, _dt.date):
                return value.isoformat()[:10]
            return _dt.date.fromisoformat(str(value).strip()).isoformat()
    except (TypeError, ValueError):
        pass
    raise SpecError(f"param '{name}': expected {type_}, got {value!r}")


def resolve_params(declared: Mapping[str, Any], given: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate run parameters against their declarations: unknown, missing and mistyped
    values raise SpecError before anything runs."""
    specs = {k: v if isinstance(v, ParamSpec) else _param_spec(k, v) for k, v in declared.items()}
    given = dict(given or {})
    unknown = sorted(set(given) - set(specs))
    if unknown:
        known = ", ".join(sorted(specs)) or "none declared"
        raise SpecError(f"unknown param(s) {unknown} (declared: {known})")
    out: dict[str, Any] = {}
    missing = []
    for name, p in specs.items():
        if name in given and given[name] is not None:
            out[name] = coerce_param(name, p.type, given[name])
        elif p.default is not None:
            out[name] = p.default
        elif p.is_required:
            missing.append(name)
        else:
            out[name] = None
    if missing:
        raise SpecError(f"missing required param(s) {missing}: pass --param {missing[0]}=...")
    return out


def _param_spec(name: str, raw: Any) -> ParamSpec:
    if isinstance(raw, str) and raw in PARAM_TYPES:
        raw = {"type": raw}
    elif not isinstance(raw, dict):
        raw = {"default": raw, "type": _infer_type(raw)}
    try:
        return ParamSpec.model_validate(raw)
    except SpecError:
        raise
    except ValueError as e:
        raise SpecError(f"param '{name}': {e}") from e


def _infer_type(value: Any) -> str:
    return {bool: "bool", int: "int", float: "float"}.get(type(value), "str")


def substitute_params(value: Any, params: Mapping[str, Any]) -> Any:
    """Replace ${params.x} recursively. A string that is exactly one reference keeps the
    parameter's type (an int stays an int); inside a longer string it is formatted as text."""
    if isinstance(value, str):
        whole = PARAM_REF.fullmatch(value)
        if whole and whole.group(1) in params:
            return params[whole.group(1)]

        def sub(m: re.Match) -> str:
            if m.group(1) not in params:
                raise SpecError(f"{m.group(0)} is not declared under params:")
            v = params[m.group(1)]
            return "" if v is None else str(v).lower() if isinstance(v, bool) else str(v)

        return PARAM_REF.sub(sub, value)
    if isinstance(value, list):
        return [substitute_params(v, params) for v in value]
    if isinstance(value, dict):
        return {k: substitute_params(v, params) for k, v in value.items()}
    return value


class PipelineSpec(BaseModel):
    name: str
    # Free-form str -> str tags (team, domain, ...) recorded with each run; see agentl.labels.
    labels: dict[str, str] = Field(default_factory=dict)
    window: WindowSpec | None = None

    # Run parameters, used as ${params.name}. Values come from --param, params= or serve.
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    # Under `agentl serve`: what starts a run. A `use: trigger` step gets what started it.
    trigger: list[TriggerSpec] = Field(default_factory=list)
    llm: dict[str, LLMSpec] = Field(default_factory=lambda: {"default": LLMSpec()})
    steps: list[StepSpec]
    notify: NotifySpec | None = None
    # Under `agentl serve`: what to do when a run ends (start another pipeline, emit an event).
    on_success: list[ActionSpec] = Field(default_factory=list)
    on_failure: list[ActionSpec] = Field(default_factory=list)
    after: list[ActionSpec] = Field(default_factory=list)  # either outcome
    budget: BudgetSpec | None = None
    outcomes: OutcomesSpec = Field(default_factory=OutcomesSpec)
    # After a failed run whose error looks like an agentl bug; see agentl.report.
    report: ReportSpec = Field(default_factory=ReportSpec)
    concurrency: ConcurrencySpec | None = None
    # OpenLineage events for data catalogs; see agentl.lineage.
    lineage: LineageSpec | None = None
    # Independent steps run together, up to this many; 1 = one at a time.
    max_parallel_steps: int = Field(default=4, ge=1, le=64)
    # Stream linear chains (reader -> row-wise steps -> writer) this many records at a time.
    chunk_size: int | None = Field(default=None, ge=1)
    # Pass pyarrow Tables between steps that can take them. None: when pyarrow is installed.
    arrow: bool | None = None
    mcp: dict[str, MCPServerSpec] = Field(default_factory=dict)
    # Partitions the destination should hold; `agentl gaps` lists the missing ones.
    expect: ExpectSpec | None = None
    # Folder of the YAML file; `fn:` modules are looked up there first. Set by `load`.
    base_dir: str | None = Field(default=None, exclude=True)

    @field_validator("labels", mode="before")
    @classmethod
    def _labels(cls, v: Any) -> Any:
        from .labels import check

        if v is None:
            return {}
        if not isinstance(v, dict):
            raise ValueError("labels: must be a map of key: value strings")
        return check(v)

    @model_validator(mode="after")
    def _refs(self) -> PipelineSpec:
        if self.budget and self.budget.max_usd is not None:
            used = {n for s in self.steps if s.kind == "agent" for n in s.llms}
            used |= {f for n in used if n in self.llm for f in self.llm[n].fallback}
            unpriced = sorted(n for n in used if n in self.llm and self.llm[n].price is None)
            free = ("echo", "ollama", "openrouter")  # openrouter reports the cost of each call
            unpriced = [n for n in unpriced if self.llm[n].provider not in free]
            if unpriced:
                raise ValueError(f"budget.max_usd needs a price on llm {unpriced} (price: {{input, output}})")
        for name, spec in self.llm.items():
            for f in spec.fallback:
                if f == name or f not in self.llm:
                    raise ValueError(f"llm '{name}': fallback '{f}' must name another llm")
                if self.llm[f].fallback:
                    raise ValueError(f"llm '{name}': fallback '{f}' has its own fallback; list them all here")
        ids = [s.id for s in self.steps]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"duplicate step ids: {sorted(dup)}")
        self._routers(ids)
        for s in self.steps:
            for ref in s.inputs:
                if ref not in ids:
                    raise ValueError(f"step '{s.id}': unknown input '{ref}'")
                if not self.step(ref).required:
                    raise ValueError(
                        f"step '{s.id}': input '{ref}' is best effort (required: false); "
                        "use 'after' to order steps after it"
                    )
            for ref in s.after:
                if ref not in ids or ref == s.id:
                    raise ValueError(f"step '{s.id}': after names unknown step '{ref}'")
            for name in s.llms if s.kind == "agent" else []:
                if name not in self.llm:
                    raise ValueError(f"step '{s.id}': unknown llm '{name}'")
            if s.on_drift == "remap" and not any(
                t.use == "map_schema" and s.id in t.inputs for t in self.steps
            ):
                raise ValueError(
                    f"step '{s.id}': on_drift remap hands the new layout to a map_schema step; "
                    f"add one with input: {s.id}"
                )
            if s.check and "rejected_rate" in s.expect:
                from .checks import rejected_limits

                above = _upstream(self.steps, s.id)
                for ref in rejected_limits(s.expect["rejected_rate"], s.check):
                    if ref not in above:
                        raise ValueError(
                            f"step '{s.id}': rejected_rate names '{ref}', which is not upstream of this check"
                        )
            for t in s.tools:
                name = t if isinstance(t, str) else next(iter(t))
                server = name[4:].split("/", 1)[0]
                if name.startswith("mcp:") and server not in self.mcp:
                    raise ValueError(f"step '{s.id}': tool '{name}' needs an mcp server named '{server}'")
        return self

    def _routers(self, ids: list[str]) -> None:
        """Router targets (#299): each must exist and read the router (a target with no input gets
        it), never be the router's ancestor, and take one branch only."""
        routers = [s for s in self.steps if s.routes is not None]
        for r in routers:
            for target in r.targets:
                if target not in ids:
                    raise ValueError(f"step '{r.id}': route target '{target}' does not exist (or use drop)")
                t = self.step(target)
                if t.input is None and not (t.check or t.wait_until):
                    t.input = r.id
                if r.id not in t.inputs:
                    raise ValueError(
                        f"step '{r.id}': route target '{target}' must read the router (input: {r.id}); "
                        "it receives only its branch"
                    )
                if t.kind in ("check", "wait"):
                    raise ValueError(
                        f"step '{r.id}': route target '{target}' must be a use, fn or agent step"
                    )
        if routers:
            deps = {s.id: set(s.depends) & set(ids) for s in self.steps}
            done: set[str] = set()
            while len(done) < len(deps):
                ready = {i for i in deps if i not in done and deps[i] <= done}
                if not ready:
                    raise ValueError(f"cycle between steps: {sorted(set(deps) - done)}")
                done |= ready

    def step(self, step_id: str) -> StepSpec:
        return next(s for s in self.steps if s.id == step_id)


def _upstream(steps: list[StepSpec], step_id: str) -> set[str]:
    by_id = {s.id: s for s in steps}
    seen: set[str] = set()
    todo = list(by_id[step_id].inputs)
    while todo:
        cur = todo.pop()
        if cur in seen or cur not in by_id:
            continue
        seen.add(cur)
        todo.extend(by_id[cur].inputs)
    return seen


def interpolate(value: Any, env: Mapping[str, str] | None = None) -> Any:
    """Replace ${VAR} and ${VAR:-default} recursively, and secret references such as
    ${aws-sm:prod/pg_dsn} (see agentl.secrets). Missing vars raise."""
    env = os.environ if env is None else env
    if isinstance(value, str):
        from . import secrets

        if found := secrets.SECRET.search(value):
            try:
                value = secrets.substitute(value)
            except Exception as e:
                raise SpecError(f"could not read secret {found.group(0)!r}: {e}") from e

        def sub(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            if name in env:
                secrets.remember_env(name, env[name])
                return env[name]
            if default is not None:
                return default
            raise SpecError(f"environment variable '{name}' is not set")

        return _ENV.sub(sub, value)
    if isinstance(value, list):
        return [interpolate(v, env) for v in value]
    if isinstance(value, dict):
        return {k: interpolate(v, env) for k, v in value.items()}
    return value


def _flatten_sections(raw: dict) -> dict:
    """Allow extract/transform/load sugar. In transform/load, a step without
    `input` reads from the previous step."""
    sections = [k for k in ("extract", "transform", "load") if k in raw]
    if not sections:
        return raw
    if "steps" in raw:
        raise SpecError("use either 'steps' or extract/transform/load, not both")
    steps: list[dict] = []
    for section in sections:
        items = raw.pop(section) or []
        if isinstance(items, dict):
            items = [items]
        for i, item in enumerate(items):
            item = dict(item)
            item.setdefault("id", f"{section}_{i + 1}")
            if section != "extract" and "input" not in item and steps:
                item["input"] = steps[-1]["id"]
            steps.append(item)
    raw["steps"] = steps
    return raw


def parse(
    raw: dict, env: Mapping[str, str] | None = None, params: Mapping[str, Any] | None = None
) -> PipelineSpec:
    """Validate a pipeline mapping. `params` are checked against `params:` (unknown, missing
    and mistyped values raise SpecError) and substituted for ${params.x}."""
    raw = dict(raw)
    declared = raw.pop("params", None) or {}
    if not isinstance(declared, dict):
        raise SpecError("params: expected a mapping of name -> type or {type, default}")
    specs = {k: _param_spec(k, v) for k, v in interpolate(declared, env).items()}
    values = resolve_params(specs, params)
    raw = substitute_params(interpolate(raw, env), values)
    raw["params"] = {k: v.model_dump(exclude_none=True) for k, v in specs.items()}
    raw = _flatten_sections(raw)
    if isinstance(raw.get("llm"), dict) and "provider" in raw["llm"]:
        raw["llm"] = {"default": raw["llm"]}
    try:
        return PipelineSpec.model_validate(raw)
    except ValueError as e:
        raise SpecError(str(e)) from e


def load(
    path: str | Path, env: Mapping[str, str] | None = None, params: Mapping[str, Any] | None = None
) -> PipelineSpec:
    text = Path(path).read_text(encoding="utf-8")
    try:
        raw = yaml.load(text, Loader=_Loader)
    except yaml.YAMLError as e:
        hint = ""
        if "'?'" in str(e):
            hint = "\nhint: optional types like `str?` need block style or quotes inside { }"
        raise SpecError(f"{path}: invalid YAML: {e}{hint}") from None
    if not isinstance(raw, dict):
        raise SpecError(f"{path}: expected a mapping at the top level")
    spec = parse(raw, env, params)
    spec.base_dir = str(Path(path).resolve().parent)
    return spec
