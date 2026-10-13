<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="media/brand/dist/agentl-wordmark-dark.svg">
    <img src="media/brand/dist/agentl-wordmark-light.svg" alt="agentl" height="72">
  </picture>
</p>

[![CI](https://github.com/arimayuji/agentl/actions/workflows/ci.yml/badge.svg)](https://github.com/arimayuji/agentl/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/agentl)](https://pypi.org/project/agentl/)
[![Python](https://img.shields.io/pypi/pyversions/agentl)](https://pypi.org/project/agentl/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Agentic ETL for Python. Declare pipelines in YAML where each step is a connector, a function or an LLM agent. Read CSV, Excel, PDF, Word, HTTP and SQL, validate agent outputs with schemas, and follow every run in a live diagram, a local dashboard and notifications.

> Status: early (v0.1). The API may change.

**Documentation: [agentl.com.br](https://agentl.com.br)**

## Install

```bash
pip install agentl                 # core: CSV, JSON, HTTP, SQLite, runner, dashboard
pip install 'agentl[office,pdf]'   # Excel, Word, PDF
pip install 'agentl[anthropic]'    # Claude as the LLM provider
pip install 'agentl[all]'
```

Until it is on PyPI: `pip install git+https://github.com/arimayuji/agentl`.

## A pipeline

```yaml
name: notas-fiscais
llm:
  provider: anthropic          # or "echo" to run offline
  model: claude-opus-5-5

steps:
  - id: notas
    use: csv.read
    with: { path: data/notas.csv }

  - id: clientes
    use: json.read
    with: { path: data/clientes.json }

  - id: cruzar
    use: join
    input: [notas, clientes]
    with: { left: notas, right: clientes, on: cnpj, how: left }

  - id: classificar
    agent: Classify the expense category and flag unknown clients for review.
    input: cruzar
    output: { numero: str, cliente: str?, categoria: str?, revisar: bool? }

  - id: salvar
    use: sqlite.write
    input: classificar
    with: { db: out/notas.db, table: notas }

notify:
  channels:
    slack: { webhook: "${SLACK_WEBHOOK}" }
  rules:
    - on: run.failed
      to: [slack]
      summary: agent             # the LLM explains the failure in 3 lines
    - on: item.rejected
      when: "rate > 0.05"
      to: [slack]
```

```bash
agentl validate pipeline.yaml
agentl run pipeline.yaml --show classificar
agentl diagram pipeline.yaml      # Mermaid
agentl dashboard                  # http://127.0.0.1:7070
```

You can also write `extract:`, `transform:` and `load:` sections instead of `steps:`. Inside `transform` and `load`, a step without `input` reads from the previous one.

## Concepts

**Everything is a step.** A step gets records (`list[dict]`) from its inputs and returns records. Extract, transform and load are labels, not different machinery. Steps form a graph, so ETL, ELT, fan-out and joins all fit.

| Step kind | YAML | Use for |
|---|---|---|
| Connector | `use: pdf.read` | Reading and writing files, APIs and databases |
| Function | `fn: my_module:clean` | Your own deterministic code |
| Agent | `agent: "..."` + `output:` | Semantic work: classify, extract, normalize, audit |

**Agent steps** run once per record, in parallel (`concurrency`), and must declare an `output` schema (`str`, `int`, `float`, `bool`, `list[str]`, `dict`; a trailing `?` means optional). Outputs are validated; a record that fails twice is rejected, not the whole run, and shows up as an `item.rejected` event. Results are cached per record in `.agentl/agentl.db`, so re-running after a crash only pays for what is left.

**Secrets** come from the environment: `${VAR}` or `${VAR:-default}`. Never put them in the YAML.

## Built-in connectors

`csv.read` `csv.write` `json.read` `json.write` `excel.read` `pdf.read` `docx.read` `http.get` `http.post` `sqlite.read` `sqlite.write` `select` `filter` `dedupe` `join` `concat`

`agentl connectors` lists what is installed. `http.get` accepts `{field}` placeholders in the URL to make one request per upstream record, plus `items: data.results` to pick the list out of the response.

## Extending

```python
from agentl import connector, register_provider


@connector("crm.contacts")
def contacts(input, since=None):
    return [{"id": 1, "email": "a@b.com"}]
```

Packages can register connectors through the `agentl.connectors` entry point group, so `pip install agentl-yourthing` is enough. LLM providers plug in with `register_provider(name, factory)`; the factory returns an object with `structured(instruction, record, schema)` and `text(instruction, content)`.

## Events

The runner emits `run.started`, `step.started`, `step.succeeded`, `step.failed`, `item.rejected`, `source.empty`, `run.succeeded` and `run.failed`. They feed the store, the dashboard and notification rules (`slack`, `webhook`, `console`).

## Roadmap

- Connectors: S3/R2, email (IMAP), Postgres, Playwright, AWS Lambda
- Agent tools (HTTP, SQL, browser) and MCP servers as tools
- More providers (OpenAI, Ollama, Bedrock), cost budget per run
- `agentl new "describe the pipeline"` and an MCP server to operate pipelines from Claude Code

## Contributing

Bug reports, connector requests and pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md)
for the setup and the checks every PR must pass, and [SECURITY.md](SECURITY.md) to report vulnerabilities privately.
Release history is in [CHANGELOG.md](CHANGELOG.md).

## Sponsors

agentl is free and MIT licensed. If it saves you time, consider [sponsoring the project](https://github.com/sponsors/arimayuji).

## License

MIT
