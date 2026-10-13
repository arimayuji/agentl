"""Pipeline spec: YAML -> validated models."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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


class LLMSpec(BaseModel):
    provider: str = "anthropic"
    model: str = "claude-opus-5-5"
    max_tokens: int = 4096
    options: dict[str, Any] = Field(default_factory=dict)


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
    cache: bool | None = None  # default: True for agent steps

    @model_validator(mode="after")
    def _one_kind(self) -> StepSpec:
        kinds = [k for k in ("use", "fn", "agent") if getattr(self, k)]
        if len(kinds) != 1:
            raise ValueError(f"step '{self.id}': set exactly one of use/fn/agent")
        if self.agent and not self.output:
            raise ValueError(f"step '{self.id}': agent steps need an 'output' schema")
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

    @model_validator(mode="after")
    def _refs(self) -> PipelineSpec:
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
    raw = yaml.load(text, Loader=_Loader)
    if not isinstance(raw, dict):
        raise SpecError(f"{path}: expected a mapping at the top level")
    return parse(raw, env)
