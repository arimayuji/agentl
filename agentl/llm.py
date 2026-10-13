"""LLM providers. A provider turns (instruction, record, schema) into a
validated object. Register more with `register_provider`."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, create_model

from .spec import LLMSpec

_TYPES: dict[str, Any] = {"str": str, "int": int, "float": float, "bool": bool, "list": list, "dict": dict}


def schema_model(name: str, fields: dict[str, str]) -> type[BaseModel]:
    """{"valor": "float", "obs": "str?"} -> pydantic model. `?` = optional."""
    defs: dict[str, Any] = {}
    for field, type_name in fields.items():
        optional = type_name.endswith("?")
        base = type_name.rstrip("?")
        if base.startswith("list[") and base.endswith("]"):
            py = list[_TYPES[base[5:-1]]]  # type: ignore[misc,valid-type]
        elif base in _TYPES:
            py = _TYPES[base]
        else:
            raise ValueError(f"unknown type '{type_name}' for field '{field}'")
        defs[field] = (py | None, None) if optional else (py, ...)
    return create_model(name, **defs)


@dataclass
class Result:
    value: BaseModel
    input_tokens: int = 0
    output_tokens: int = 0


class Provider(Protocol):
    def structured(self, instruction: str, record: dict, schema: type[BaseModel]) -> Result: ...
    def text(self, instruction: str, content: str) -> str: ...


SYSTEM = (
    "You are one step of a data pipeline. Apply the instruction to the record "
    "you receive and answer only with the requested fields. If a value is not "
    "present in the record, use null for optional fields instead of inventing it."
)


class AnthropicProvider:
    def __init__(self, spec: LLMSpec):
        try:
            import anthropic
        except ImportError as e:
            raise ImportError("pip install 'agentl[anthropic]'") from e
        self.client = anthropic.Anthropic()
        self.spec = spec

    def structured(self, instruction: str, record: dict, schema: type[BaseModel]) -> Result:
        msg = f"Instruction: {instruction}\n\nRecord:\n{json.dumps(record, ensure_ascii=False, default=str)}"
        resp = self.client.messages.parse(
            model=self.spec.model,
            max_tokens=self.spec.max_tokens,
            system=SYSTEM,
            messages=[{"role": "user", "content": msg}],
            output_format=schema,
            **self.spec.options,
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError("model refused this record")
        return Result(resp.parsed_output, resp.usage.input_tokens, resp.usage.output_tokens)

    def text(self, instruction: str, content: str) -> str:
        resp = self.client.messages.create(
            model=self.spec.model,
            max_tokens=self.spec.max_tokens,
            messages=[{"role": "user", "content": f"{instruction}\n\n{content}"}],
            **self.spec.options,
        )
        return "".join(b.text for b in resp.content if b.type == "text")


class EchoProvider:
    """Offline provider for tests and dry runs: copies matching fields from
    the record. Costs nothing and needs no API key."""

    def __init__(self, spec: LLMSpec):
        self.spec = spec

    def structured(self, instruction: str, record: dict, schema: type[BaseModel]) -> Result:
        return Result(schema.model_validate({k: record.get(k) for k in schema.model_fields}))

    def text(self, instruction: str, content: str) -> str:
        return content[:500]


_PROVIDERS: dict[str, Callable[[LLMSpec], Provider]] = {
    "anthropic": AnthropicProvider,
    "echo": EchoProvider,
}


def register_provider(name: str, factory: Callable[[LLMSpec], Provider]) -> None:
    _PROVIDERS[name] = factory


def provider(spec: LLMSpec) -> Provider:
    if spec.provider not in _PROVIDERS:
        raise KeyError(f"unknown llm provider '{spec.provider}'. Available: {sorted(_PROVIDERS)}")
    return _PROVIDERS[spec.provider](spec)
