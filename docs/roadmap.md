# Roadmap

This is where agentl is heading. Every item is a GitHub issue: add a :+1: to the ones you need,
comment with your use case, or pick one up and open a pull request.
Priorities change with feedback, so dates are intentionally left out.

## Released

- **0.12.0**: agents that check each other and a project that is easy to join. `critic:`,
  router `routes:` and `jury:` on agent steps; step `retries` and `fallback`; `agentl repair`
  proposes a checked patch for a failed run; `agentl cost` splits compute cost by pipeline, step
  and model; run `labels:` with a grouped dashboard and `agentl dashboard export`; layout drift
  detection on checks; `agentl report` drafts a bug report. Also a hardened CI, repository bots and
  the new blue cube logo.
- **0.11.0**: data cleaning and safe re-runs. `cast` with semantic types (dates with one day/month
  order per column, money, Brazilian and US documents, phones, enums); `compute` with a safe
  expression language; `aggregate`, `sort`, `limit`, `fill`, `flatten`, `explode`, `pivot`,
  `unpivot`; `join` on several keys and outer; `dedupe keep`. Rejected records go to a quarantine
  (`agentl rejected list|show|retry`). `postgres.write` and `sqlite.write` upserts take
  `on_conflict` and `update_columns` and report inserted, updated and unchanged rows. Also polars
  steps, `agentl mcp install` for five clients and a performance gate in CI. Sensitive fields can be
  encrypted, hashed, masked or dropped (`protect`), and IoT data arrives through MQTT, Kinesis and
  Event Hubs, aggregated in time windows.
- **0.10.0**: messaging everywhere, with a `.send` step and a `notify:` channel for WhatsApp, SMS,
  Slack, Teams, Telegram, Discord, Google Chat, Mattermost, ntfy, Pushover, Matrix and email. Also
  `agentl new --template` (the tested recipes, no LLM call), `agentl run` checking the pipeline
  first, `agentl connectors <name>` and `sqlite.write` upsert. The agent benchmark gains jobs that
  run twice.
- **0.9.0**: for people and AI agents alike: `agentl validate` and `agentl lint` with every error,
  its line and its fix; `validate --connect` and `agentl plan` (calls and cost before a run);
  `agentl preview`; a short Python API for one-off jobs (`agentl.read`, `write`,
  `http_get_all`); a Claude Code skill; new MCP tools; 20 tested recipes; an agent benchmark; and
  the MCP registry listing.
- **0.8.1**: fixes from a first hands-on test: `agentl --version`, HTTPS on macOS Python without
  root certificates, records written by writers, `agentl serve` output, `agentl new` model choice,
  and `join` keeping same-name columns.
- **0.8.0**: OpenRouter provider, fallback between LLM providers, model cascade (cheap model first,
  strong one for the records it cannot answer), identical records asked once, incremental loading
  with a persisted cursor (`agentl state`) and a release-by-button workflow.
- **0.7.0**: agent tools and MCP servers, data checks, Excel and versioned outputs, web pages as
  Markdown, `agentl mcp` / `new` / `doctor` / `stats` / `serve` (schedules, webhooks, new files),
  `wait_until`, Google Sheets and Drive, SharePoint and OneDrive, GraphQL, Kafka / SQS / RabbitMQ /
  Pub/Sub, secret references for AWS / GCP / Azure, Azure OpenAI, benchmarks against plain Python,
  evals, before-and-after cases and a security hardening pass.
- **0.6.1**: fixes copying and pickling `RunResult` and outputs that could show Arrow tables.
- **0.6.0**: performance: reproducible benchmarks, prompt caching, several records per call and
  batch mode for agent steps, independent steps in parallel, chunked streaming for large inputs and
  an Arrow fast path (10x on CSV to pandas to Parquet, 22x on Parquet to Postgres).
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

## Later

Everything on the earlier list has shipped. Ideas and requests go to [issues](https://github.com/arimayuji/agentl/issues).

## Missing something?

Open a [feature request](https://github.com/arimayuji/agentl/issues/new?template=feature_request.yml) or a
[connector request](https://github.com/arimayuji/agentl/issues/new?template=connector_request.yml).
