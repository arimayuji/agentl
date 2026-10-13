# Roadmap

This is where agentl is heading. Every item is a GitHub issue: add a :+1: to the ones you need,
comment with your use case, or pick one up and open a pull request.
Priorities change with feedback, so dates are intentionally left out.

## Released

- **0.1.1**: YAML pipelines, agent steps with validated schemas, per-record cache, connectors
  (CSV, JSON, Excel, PDF, Word, HTTP, SQLite), notifications, CLI and local dashboard.
  See the [changelog](changelog.md).

## Next: 0.2, real-world pipelines

| Feature | Issue |
|---|---|
| Read CSV and JSON directly from a URL | [#7](https://github.com/arimayuji/agentl/issues/7) |
| Universal file reader: txt, xls, zip/gz, parquet, xml, html, legacy .doc | [#22](https://github.com/arimayuji/agentl/issues/22) |
| pandas bridge: steps that receive and return DataFrames | [#8](https://github.com/arimayuji/agentl/issues/8) |
| Postgres connectors with upsert | [#9](https://github.com/arimayuji/agentl/issues/9) |
| Docs for AI agents: `llms.txt`, prompt library, `AGENTS.md` | [#10](https://github.com/arimayuji/agentl/issues/10) |
| Guide: migrate a pandas pipeline (CSV over HTTP to Postgres) | [#11](https://github.com/arimayuji/agentl/issues/11) |

## Then: 0.3, more sources and providers

| Feature | Issue |
|---|---|
| Object storage: S3, R2 and GCS | [#12](https://github.com/arimayuji/agentl/issues/12) |
| Email (IMAP) with attachments | [#13](https://github.com/arimayuji/agentl/issues/13) |
| Browser connector with Playwright | [#14](https://github.com/arimayuji/agentl/issues/14) |
| AWS Lambda connector | [#15](https://github.com/arimayuji/agentl/issues/15) |
| More LLM providers: OpenAI, Ollama, Bedrock | [#16](https://github.com/arimayuji/agentl/issues/16) |
| Cost budget per run | [#17](https://github.com/arimayuji/agentl/issues/17) |

## Later

| Feature | Issue |
|---|---|
| Agent tools and MCP servers inside agent steps | [#18](https://github.com/arimayuji/agentl/issues/18) |
| MCP server and `agentl new` to build pipelines from a description | [#19](https://github.com/arimayuji/agentl/issues/19) |
| Triggers: schedules, webhooks and file arrivals | [#20](https://github.com/arimayuji/agentl/issues/20) |

## Missing something?

Open a [feature request](https://github.com/arimayuji/agentl/issues/new?template=feature_request.yml) or a
[connector request](https://github.com/arimayuji/agentl/issues/new?template=connector_request.yml).
