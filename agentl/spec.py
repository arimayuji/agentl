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


class StepSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str
    use: str | None = None
    fn: str | None = None
    agent: str | None = None
    input: str | list[str] | None = None
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    output: dict[str, str] | None = None
    llm: str = "default"
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

    @model_validator(mode="after")
    def _one_kind(self) -> StepSpec:
        kinds = [k for k in ("use", "fn", "agent") if getattr(self, k)]
        if len(kinds) != 1:
            raise ValueError(f"step '{self.id}': set exactly one of use/fn/agent")
        if self.agent and not self.output:
            raise ValueError(f"step '{self.id}': agent steps need an 'output' schema")
        if self.mode == "batch" and self.records_per_call > 1:
            raise ValueError(
                f"step '{self.id}': mode batch sends one record per request; drop records_per_call"
            )
        if self.tools and not self.agent:
            raise ValueError(f"step '{self.id}': only agent steps take tools")
        if self.tools and (self.mode == "batch" or self.records_per_call > 1):
            raise ValueError(
                f"step '{self.id}': tools work one record per call; drop mode batch and records_per_call"
            )
        for t in self.tools:
            if isinstance(t, dict) and len(t) != 1:
                raise ValueError(f"step '{self.id}': write each tool with settings as its own item")
        if self.records_per_call > 1 and "index" in (self.output or {}):
            raise ValueError(f"step '{self.id}': with records_per_call, 'index' is reserved in output")
        return self

    @property
    def kind(self) -> str:
        return "use" if self.use else "fn" if self.fn else "agent"

    @property
    def inputs(self) -> list[str]:
        if self.input is None:
            return []
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


class PipelineSpec(BaseModel):
    name: str
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
            used = {s.llm for s in self.steps if s.kind == "agent"}
            unpriced = sorted(n for n in used if n in self.llm and self.llm[n].price is None)
            unpriced = [n for n in unpriced if self.llm[n].provider not in ("echo", "ollama")]
            if unpriced:
                raise ValueError(f"budget.max_usd needs a price on llm {unpriced} (price: {{input, output}})")
        ids = [s.id for s in self.steps]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"duplicate step ids: {sorted(dup)}")
        for s in self.steps:
            for ref in s.inputs:
                if ref not in ids:
                    raise ValueError(f"step '{s.id}': unknown input '{ref}'")
            if s.kind == "agent" and s.llm not in self.llm:
                raise ValueError(f"step '{s.id}': unknown llm '{s.llm}'")
            for t in s.tools:
                name = t if isinstance(t, str) else next(iter(t))
                server = name[4:].split("/", 1)[0]
                if name.startswith("mcp:") and server not in self.mcp:
                    raise ValueError(f"step '{s.id}': tool '{name}' needs an mcp server named '{server}'")
        return self

    def step(self, step_id: str) -> StepSpec:
        return next(s for s in self.steps if s.id == step_id)


def interpolate(value: Any, env: Mapping[str, str] | None = None) -> Any:
    """Replace ${VAR} and ${VAR:-default} recursively. Missing vars raise."""
    env = os.environ if env is None else env
    if isinstance(value, str):

        def sub(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            if name in env:
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


def parse(raw: dict, env: dict[str, str] | None = None) -> PipelineSpec:
    raw = interpolate(dict(raw), env)
    raw = _flatten_sections(raw)
    if isinstance(raw.get("llm"), dict) and "provider" in raw["llm"]:
        raw["llm"] = {"default": raw["llm"]}
    try:
        return PipelineSpec.model_validate(raw)
    except ValueError as e:
        raise SpecError(str(e)) from e


def load(path: str | Path, env: dict[str, str] | None = None) -> PipelineSpec:
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
    spec = parse(raw, env)
    spec.base_dir = str(Path(path).resolve().parent)
    return spec
