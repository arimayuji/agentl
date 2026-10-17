"""Pipeline spec: YAML -> validated models."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    "rejected_rate",
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
    review: str | None = None
    on_fail: Literal["stop", "warn"] = "stop"
    # `rules:` depends on the step kind (validated in _one_kind):
    # - on a `check` step (#218): per-row rules (RowRuleSpec), with `identity` naming the columns
    #   that identify a finding from one day to the next (default: the rule's column value);
    # - on an `agent` step (#124, #195, hybrid): rules tried in order on each record before the
    #   agent; a record whose rules all succeed and give valid `output:` fields never reaches the
    #   llm. Items are one-key dicts: {compute: {field: expr}}, {cast: {field: type}} or
    #   {fn: "module:function"}.
    rules: list[Any] = Field(default_factory=list)
    identity: list[str] = Field(default_factory=list)
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
    concurrency: int = 4
    records_per_call: int = Field(default=1, ge=1, le=200)
    mode: Literal["sync", "batch"] = "sync"
    batch_poll_seconds: float = Field(default=30, gt=0)
    batch_timeout_seconds: float = Field(default=86400, gt=0)
    cache: bool | None = None  # default: True for agent steps
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

    @model_validator(mode="before")
    @classmethod
    def _cascade(cls, raw: Any) -> Any:
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
        if self.check and not (self.expect or self.review or self.rules):
            raise ValueError(f"step '{self.id}': a check needs 'expect', 'review' or 'rules'")
        if self.identity and not self.check:
            raise ValueError(f"step '{self.id}': identity belongs to check steps (with per-row rules)")
        if self.rules and not (self.check or self.agent):
            raise ValueError(
                f"step '{self.id}': rules go on an agent step (hybrid: rules first, the llm for the "
                "rows they miss) or on a check step (per-row check rules)"
            )
        if self.check:
            self.rules = [r if isinstance(r, RowRuleSpec) else _row_rule(self.id, r) for r in self.rules]
        unknown = [k for k in self.expect if k not in _EXPECTATIONS]
        if unknown:
            raise ValueError(
                f"step '{self.id}': unknown expectation {unknown}. Use {', '.join(_EXPECTATIONS)}"
            )
        if (self.expect or self.review) and not self.check:
            raise ValueError(f"step '{self.id}': expect and review belong to check steps")
        if self.agent and not self.output:
            raise ValueError(f"step '{self.id}': agent steps need an 'output' schema")
        if self.mode == "batch" and self.records_per_call > 1:
            raise ValueError(
                f"step '{self.id}': mode batch sends one record per request; drop records_per_call"
            )
        for rule in self.rules if self.agent else []:
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
        if self.tools and (self.mode == "batch" or self.records_per_call > 1):
            raise ValueError(
                f"step '{self.id}': tools work one record per call; drop mode batch and records_per_call"
            )
        for t in self.tools:
            if isinstance(t, dict) and len(t) != 1:
                raise ValueError(f"step '{self.id}': write each tool with settings as its own item")
        if self.cursor and not (self.use or self.fn):
            raise ValueError(f"step '{self.id}': cursor goes on a use or fn step that reads the data")
        if self.escalate and not self.agent:
            raise ValueError(f"step '{self.id}': only agent steps take a list of llms")
        if self.escalate and self.mode == "batch":
            raise ValueError(f"step '{self.id}': a cascade of llms does not work with mode batch")
        if self.escalate_below is not None:
            if not self.escalate:
                raise ValueError(
                    f"step '{self.id}': escalate_below needs a list of llms, e.g. llm: [cheap, strong]"
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
        return self

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


class NotifySpec(BaseModel):
    channels: dict[str, Channel] = Field(default_factory=dict)
    rules: list[Rule] = Field(default_factory=list)


class TriggerSpec(BaseModel):
    """What starts a run under `agentl serve`: a cron schedule, a webhook path or new files."""

    model_config = ConfigDict(extra="forbid")

    cron: str | None = None  # "0 7 * * 1-5", local time unless `timezone`
    timezone: str | None = None
    webhook: str | None = None  # "/hooks/invoices"
    secret: str | None = None  # webhook: Bearer token or HMAC-SHA256 signature key
    files: str | None = None  # glob, local or remote: "inbox/*.csv", "s3://bucket/in/*.xlsx"
    every: str | float = "1m"  # files: how often to look; alone: run again at this interval
    storage_options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one(self) -> TriggerSpec:
        kinds = [k for k in ("cron", "webhook", "files") if getattr(self, k)]
        if not kinds and "every" in self.model_fields_set:
            kinds = ["every"]  # on its own: run again every `every`, never two at once
        if len(kinds) != 1:
            raise ValueError("a trigger has exactly one of cron, webhook, files or every")
        if self.cron:
            from .triggers import Cron

            Cron(self.cron)
        if self.webhook and not self.webhook.startswith("/"):
            raise ValueError(f"webhook path must start with '/': {self.webhook}")
        if self.secret and not self.webhook:
            raise ValueError("secret belongs to webhook triggers")
        return self

    @property
    def kind(self) -> str:
        if self.cron or self.webhook or self.files:
            return "cron" if self.cron else "webhook" if self.webhook else "files"
        return "every"


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
    # Run parameters, used as ${params.name}. Values come from --param, params= or serve.
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    # Under `agentl serve`: what starts a run. A `use: trigger` step gets what started it.
    trigger: list[TriggerSpec] = Field(default_factory=list)
    llm: dict[str, LLMSpec] = Field(default_factory=lambda: {"default": LLMSpec()})
    steps: list[StepSpec]
    notify: NotifySpec | None = None
    budget: BudgetSpec | None = None
    # Independent steps run together, up to this many; 1 = one at a time.
    max_parallel_steps: int = Field(default=4, ge=1, le=64)
    # Stream linear chains (reader -> row-wise steps -> writer) this many records at a time.
    chunk_size: int | None = Field(default=None, ge=1)
    # Pass pyarrow Tables between steps that can take them. None: when pyarrow is installed.
    arrow: bool | None = None
    mcp: dict[str, MCPServerSpec] = Field(default_factory=dict)
    # Folder of the YAML file; `fn:` modules are looked up there first. Set by `load`.
    base_dir: str | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _refs(self) -> PipelineSpec:
        if self.budget and self.budget.max_usd is not None:
            used = {n for s in self.steps if s.kind == "agent" for n in [s.llm, *s.escalate]}
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
        for s in self.steps:
            for ref in s.inputs:
                if ref not in ids:
                    raise ValueError(f"step '{s.id}': unknown input '{ref}'")
            for name in [s.llm, *s.escalate] if s.kind == "agent" else []:
                if name not in self.llm:
                    raise ValueError(f"step '{s.id}': unknown llm '{name}'")
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
