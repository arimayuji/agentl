# Roadmap

This is where agentl is heading. Every item is a GitHub issue: add a :+1: to the ones you need,
comment with your use case, or pick one up and open a pull request.
Priorities change with feedback, so dates are intentionally left out.

## Released

- **0.5.1**: security hardening: URL guard against SSRF, untrusted record data in prompts, SBOM and
  build provenance on releases, pinned actions and OpenSSF Scorecard.
- **0.5.0**: AWS Lambda connector, Bedrock provider, HTTP pagination, the `examples/invoices` migration
  example, and correct cost accounting for invalid LLM answers.
- **0.4.0**: browser (Playwright) and email (IMAP) connectors, Teams/Discord/Telegram/email/
  PagerDuty/Opsgenie notifications, JSON logs, OpenTelemetry and Sentry, spend per step in the
  dashboard.
- **0.3.0**: OpenAI, Ollama and OpenAI-compatible providers, cost budget per run, remote storage
  (S3, GCS, Azure, SFTP), any SQL database, Celery/Temporal/Airflow integrations and a shared
  Postgres run store.
- **0.2.0**: read from URLs, universal `file.read` (txt, xls, parquet, xml, html, zip/gz), Postgres
  with upsert, pandas bridge, migration guide and docs for AI agents.
- **0.1.1**: YAML pipelines, agent steps with validated schemas, per-record cache, connectors
  (CSV, JSON, Excel, PDF, Word, HTTP, SQLite), notifications, CLI and local dashboard.
  See the [changelog](changelog.md).

## Next: 0.6, performance

| Feature | Issue |
|---|---|
| Reproducible benchmarks and a published performance page | [#56](https://github.com/arimayuji/agentl/issues/56) |
| Cheaper and faster agent steps: prompt caching, multi-record calls, batch mode | [#59](https://github.com/arimayuji/agentl/issues/59) |
| Run independent steps in parallel | [#57](https://github.com/arimayuji/agentl/issues/57) |
| Stream records in chunks for large inputs | [#58](https://github.com/arimayuji/agentl/issues/58) |
| Columnar fast path with Apache Arrow | [#60](https://github.com/arimayuji/agentl/issues/60) |

## Later

| Feature | Issue |
|---|---|
| Agent tools and MCP servers inside agent steps | [#18](https://github.com/arimayuji/agentl/issues/18) |
| MCP server and `agentl new` to build pipelines from a description | [#19](https://github.com/arimayuji/agentl/issues/19) |
| Triggers: schedules, webhooks and file arrivals | [#20](https://github.com/arimayuji/agentl/issues/20) |
| Queues and streams: Kafka, RabbitMQ, SQS, Pub/Sub, MQTT | [#25](https://github.com/arimayuji/agentl/issues/25) |
| GraphQL and SaaS: Google Sheets, Drive, SharePoint | [#26](https://github.com/arimayuji/agentl/issues/26) |
| Cloud integrations: secrets managers, serverless runs, AWS / GCP / Azure services | [#29](https://github.com/arimayuji/agentl/issues/29) |

## Missing something?

Open a [feature request](https://github.com/arimayuji/agentl/issues/new?template=feature_request.yml) or a
[connector request](https://github.com/arimayuji/agentl/issues/new?template=connector_request.yml).
