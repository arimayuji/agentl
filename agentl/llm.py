"""LLM providers. A provider turns (instruction, record, schema) into a
validated object. Register more with `register_provider`."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
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
            raise ImportError("pip install 'agentl-etl[anthropic]'") from e
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


def _strict_schema(schema: type[BaseModel]) -> dict:
    """JSON schema in the shape strict structured outputs expect: every field
    required (optional ones are nullable) and no extra properties."""
    js = schema.model_json_schema()
    js["required"] = list(js.get("properties", {}))
    js["additionalProperties"] = False
    for prop in js.get("properties", {}).values():
        prop.pop("default", None)
        prop.pop("title", None)
    js.pop("title", None)
    return js


class OpenAICompatibleProvider:
    """Any server with an OpenAI-style `/chat/completions` endpoint: OpenAI, Ollama,
    vLLM, LM Studio, Groq, Together, Azure-compatible gateways. Uses only the stdlib.

    `options`: `base_url`, `api_key_env` (name of the env var with the key), `timeout`,
    `retries`; anything else (temperature, seed, ...) is sent in the request body."""

    base_url = ""
    api_key_env: str | None = None
    key_required = False

    def __init__(self, spec: LLMSpec):
        opts = dict(spec.options)
        self.base_url = str(opts.pop("base_url", self.base_url)).rstrip("/")
        if not self.base_url:
            raise ValueError(f"llm provider '{spec.provider}' needs options.base_url")
        env = opts.pop("api_key_env", self.api_key_env)
        self.api_key = os.environ.get(env) if env else None
        if self.key_required and not self.api_key:
            raise ValueError(f"llm provider '{spec.provider}' needs the {env} environment variable")
        if spec.model.startswith("claude-"):
            raise ValueError(f"llm provider '{spec.provider}': set llm.model (got '{spec.model}')")
        self.timeout = float(opts.pop("timeout", 120))
        self.retries = int(opts.pop("retries", 2))
        self.extra = opts
        self.spec = spec

    def _chat(self, messages: list[dict], **body: Any) -> dict:
        payload = {"model": self.spec.model, "messages": messages, "max_tokens": self.spec.max_tokens}
        payload.update(self.extra)
        payload.update(body)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = json.dumps(payload).encode()
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(f"{self.base_url}/chat/completions", data, headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if (e.code < 500 and e.code != 429) or attempt == self.retries:
                    detail = e.read().decode(errors="replace")[:500]
                    raise RuntimeError(f"{self.spec.provider} HTTP {e.code}: {detail}") from e
            except urllib.error.URLError:
                if attempt == self.retries:
                    raise
            time.sleep(2**attempt)
        raise RuntimeError("unreachable")

    def structured(self, instruction: str, record: dict, schema: type[BaseModel]) -> Result:
        msg = f"Instruction: {instruction}\n\nRecord:\n{json.dumps(record, ensure_ascii=False, default=str)}"
        fmt = {"name": schema.__name__, "schema": _strict_schema(schema), "strict": True}
        resp = self._chat(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": msg}],
            response_format={"type": "json_schema", "json_schema": fmt},
        )
        message = resp["choices"][0]["message"]
        if message.get("refusal"):
            raise RuntimeError(f"model refused this record: {message['refusal']}")
        usage = resp.get("usage") or {}
        return Result(
            schema.model_validate_json(message.get("content") or "{}"),
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
        )

    def text(self, instruction: str, content: str) -> str:
        resp = self._chat([{"role": "user", "content": f"{instruction}\n\n{content}"}])
        return resp["choices"][0]["message"].get("content") or ""


class OpenAIProvider(OpenAICompatibleProvider):
    base_url = "https://api.openai.com/v1"
    api_key_env = "OPENAI_API_KEY"
    key_required = True


class OllamaProvider(OpenAICompatibleProvider):
    """Local models through Ollama (https://ollama.com). No key, no cost."""

    def __init__(self, spec: LLMSpec):
        host = os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
        if "://" not in host:
            host = f"http://{host}"
        self.base_url = host.rstrip("/") + "/v1"
        super().__init__(spec)


_PROVIDERS: dict[str, Callable[[LLMSpec], Provider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "ollama": OllamaProvider,
    "openai-compatible": OpenAICompatibleProvider,
    "echo": EchoProvider,
}


def register_provider(name: str, factory: Callable[[LLMSpec], Provider]) -> None:
    _PROVIDERS[name] = factory


def provider(spec: LLMSpec) -> Provider:
    if spec.provider not in _PROVIDERS:
        raise KeyError(f"unknown llm provider '{spec.provider}'. Available: {sorted(_PROVIDERS)}")
    return _PROVIDERS[spec.provider](spec)
