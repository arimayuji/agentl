# Agent steps

An agent step applies an instruction to every record of its input. Each output record is the input
record with the fields declared in `output` added or overwritten. Records whose answer does not match
the schema are rejected (`item.rejected`), not passed on.

```yaml
- id: classify
  agent: Classify the expense category from the description.
  input: invoices
  output:
    number: str
    category: str?
    tags: list[str]?
  llm: default
  concurrency: 4
```

## Output schema

Types: `str`, `int`, `float`, `bool`, `list`, `dict`, `list[str]` (or any of the scalar types).
A trailing `?` makes the field optional.

The model's answer is validated against the schema. A record that fails validation is retried once,
then rejected: the run continues and an `item.rejected` event reports how many records failed.

## Providers

```yaml
llm:
  default: { provider: anthropic, model: claude-opus-5-5 }
  offline: { provider: echo }
```

| Provider | Notes |
|---|---|
| `anthropic` | Claude with structured output. Needs `agentl-etl[anthropic]` and `ANTHROPIC_API_KEY`. |
| `openai` | OpenAI Chat Completions with strict JSON schema. Needs `OPENAI_API_KEY`. No extra dependency. |
| `ollama` | Local models through [Ollama](https://ollama.com), free and offline. Uses `OLLAMA_HOST` (default `http://localhost:11434`). |
| `openai-compatible` | Any OpenAI-style endpoint (vLLM, LM Studio, Groq, Together, gateways): set `options.base_url` and, if needed, `options.api_key_env`. |
| `echo` | Offline. Copies matching fields from the record and leaves the rest empty, so required fields that are not in the record (for example `valid: bool`) get rejected. Mark them optional (`bool?`) while testing with echo. |

```yaml
llm:
  default: { provider: openai, model: gpt-4o-mini }
  local: { provider: ollama, model: llama3.2 }
  groq:
    provider: openai-compatible
    model: llama-3.3-70b-versatile
    options: { base_url: https://api.groq.com/openai/v1, api_key_env: GROQ_API_KEY, temperature: 0 }
```

Non-Anthropic providers need an explicit `model`. Extra `options` (temperature, seed, ...) go into the
request body; `timeout` and `retries` control the HTTP call. Structured output depends on the model:
small local models may return JSON that does not match the schema, and those records are rejected.

Each agent step chooses a provider with `llm:`. Register your own with
[`register_provider`](extending.md#llm-providers).

## Data privacy

Agent steps send record contents to the configured provider. Use a local provider for data that must
not leave your machine, and select only the fields the agent needs with a `select` step first.
