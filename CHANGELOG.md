# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version is `0.x`, minor releases may contain breaking changes.
They are always listed under **Changed** or **Removed**.

## [Unreleased]

### Added

- `agentl --version` (`-V`).
- `agentl serve` prints one line per run (pipeline, trigger, run id, status, error).

### Fixed

- HTTPS on a fresh macOS Python (python.org build without root certificates): agentl uses
  certifi's CA bundle when it is installed, and a `CERTIFICATE_VERIFY_FAILED` error now says how to
  fix it.
- `agentl new` writes the provider and model it ran with (flags or `AGENTL_PROVIDER` /
  `AGENTL_MODEL`) into the pipeline, instead of an llm the model guessed (often an old model id).
- Writer steps report how many records they wrote (`written` in `step.succeeded`), not only the one
  summary record per file.

## [0.8.0] - 2026-10-15

### Added

- Model cascade: `llm: [cheap, strong]` on an agent step sends a record to the next llm when its
  answer stays invalid, or with `escalate_below`, when its `confidence` is low. Spend per step
  reports `answered_by`. Benchmark `agent cascade`: 5.2x cheaper than one strong model.
- Agent steps send identical records once per run (`agent.deduplicated` event).
- Incremental loading: `cursor: {field, initial}` on a source step keeps the largest value seen in
  the run store and puts it in `{cursor}` on the next run; only a successful run moves it.
  `agentl state show|reset`.
- `provider: openrouter`: hundreds of models behind one key (`OPENROUTER_API_KEY`), OpenRouter's
  own model fallback (`options.models`), attribution headers, and the cost OpenRouter reports per
  call, so budgets need no `price`.
- `fallback:` on any llm: when a call fails, it goes to the next llm in the list, any provider.
  Each answer is priced with the llm that gave it; switches are logged as `llm.fallback` events.
- Release workflow can be started by hand (Actions → Release → Run workflow) with a version.

### Changed

- Agent steps no longer call the LLM twice for identical records in the same run, and their spend
  includes `answered_by`.

## [0.7.0] - 2026-10-14

### Security

- `agentl mcp`: `run_pipeline` only runs pipeline files under `--root` (default: the working
  directory) and refuses pipelines that run code unless started with `--allow-code`; `validate`
  and `diagram` no longer read environment variables or secret references.
- URL guard: connections go to the address that was checked (DNS rebinding is refused), and the
  `http.get` agent tool enforces `allow_hosts` on every redirect.
- Raised minimum versions with known vulnerabilities: pydantic 2.4, pypdf 6.19, pyarrow 23.0.1,
  requests 2.33, fsspec 2026.6, sentry-sdk 2.8, paramiko 3.4.
- Config URLs (LLM `base_url`, MCP `url`, notification webhooks) must be http(s); webhooks reject
  a bad `Content-Length`, time out idle clients and warn when served open on a public address;
  zip extraction counts the bytes actually written.
- XML files with entity declarations are refused (no "billion laughs" expansion).
- Property tests (Hypothesis) on the spec, cron, wait conditions, webhook bodies and auth, and the
  URL guard.
- The release workflow installs its tools from a hash-locked `requirements/release.txt` and
  attaches the signed provenance bundle (`*.sigstore.json`) to each GitHub release.

### Added

- `python -m cases`: four common pipelines (ticket triage, invoice extraction, web summaries,
  a daily sales report) written by hand and with agentl, checked to write the same result, with
  lines of code, time and model calls on a clean run, after a failed write and on a re-run.
  See the Before and after page.
- Secret references resolved at load time and kept out of the run store: `${aws-sm:...}`
  (with `#key` for JSON secrets), `${ssm:...}`, `${gcp-sm:...}`, `${azure-kv:...}`.
- `provider: azure-openai` (Azure OpenAI v1 API). New Cloud page with deployment recipes for
  Cloud Run jobs, Azure Container Apps jobs and AWS.
- Queues and streams: `kafka.consume` / `kafka.produce`, `sqs.receive` / `sqs.send`,
  `rabbitmq.consume` / `rabbitmq.publish`, `pubsub.pull` / `pubsub.publish`. Consumers read a
  micro-batch and acknowledge it only after the run succeeds (at-least-once); connectors can
  register this with `registry.after_run`. New trigger `every:` for back-to-back runs.
- `trigger:` in the pipeline (`cron`, `webhook` with `secret`, `files` with a cursor in the run
  store) and `agentl serve` to run pipelines on them; a `use: trigger` step gets what started the
  run. Runs of one pipeline queue instead of overlapping.
- `graphql.query` (cursor pagination), `gsheets.read` / `gsheets.write`, `gdrive.files` /
  `gdrive.upload` and `sharepoint.files` / `sharepoint.upload` (SharePoint and OneDrive through
  Microsoft Graph, chunked uploads over 4 MB). Credentials from the environment; Google service
  accounts need `agentl-etl[google]`.
- `wait_until` steps: re-run a source step every `every` until a condition holds (`rows > 0`,
  `max(date) == today`, `changed`), for at most `timeout`, then fail or continue. Events
  `wait.polled`, `wait.done` and `wait.timeout`.
- `python -m evals`: accuracy per field on two small labeled datasets, tokens and USD per 1k
  records, and cache savings on reruns, for agentl, agentl with `records_per_call: 10` and a
  plain-Python baseline with the same model and prompt. See the Evals page.
- `agentl stats`: track record per pipeline from the run store (success rate, median and p95
  duration, cost, failures grouped by step and normalized error, per-day trend), also in the
  dashboard and as the MCP tool `pipeline_stats`.
- `format: markdown` on `html.read` and `extract: markdown` on `browser.scrape`: the page's main
  content as clean Markdown (`agentl-etl[web]`, trafilatura). `evidence` keeps the HTML, the
  Markdown and a screenshot of every page, with `evidence_keep_days`; `retry_if_empty` reads a page again.
- `agentl mcp`: an MCP server over stdio with `validate`, `diagram`, `run_pipeline`, `list_runs`,
  `get_run`, `list_failures`, `doctor` and `list_connectors`, for Claude Code, Claude Desktop and Cursor.
- `agentl new "description"`: an LLM writes the pipeline YAML, validation errors go back to it, and
  the file is written only once it validates.
- `agentl doctor [run_id]`: explains a failed run from the run store; `--suggest` asks an LLM for a fix.
- `python -m benchmarks --compare`: the same jobs with agentl, in plain Python and with pandas, measuring time, CPU, peak memory and I/O, and failing unless every variant writes the same data. Results in the Performance page.
- `excel.write`: an .xlsx ready to open (bold header, frozen first row, filter, fitted columns,
  numeric text as numbers), one sheet per input step.
- Versioned outputs on every file writer: `{date}` and `{datetime}` in `path`, a `latest` copy
  under a fixed name, and `keep: N` to delete older versions (local and remote paths).
- Data checks (`check:` steps): fixed expectations (`rows`, `not_null`, `unique`, `accepted`,
  `range`, `fresh`, `vs_last_run`) and an optional agent `review` of the data's profile. With
  `on_fail: stop` a failing check stops the run before the writer; `warn` lets the data through.
  Events `check.passed` and `check.failed` with the findings. New "Data checks" docs page.
- Tools for agent steps (`tools:`): the model can call `http.get`, a read-only `sqlite.query`, Python
  functions decorated with `@tool`, and MCP servers declared under `mcp:` (stdio or streamable HTTP,
  stdlib client). Read-only by default; tools that can write must be listed in `write_tools`.
  `max_tool_calls` per record, `budget.max_tool_calls` per run, `tool_calls` in spend and a
  `tool.called` event for every call. Supported by the `anthropic`, `openai` and `openai-compatible`
  providers.

### Changed

- The quickstart example is now in English: `examples/quickstart.yaml` (was `examples/notas.yaml`)
  with `examples/data/invoices.csv` and `customers.json`, steps `invoices`, `customers`, `matched`,
  `classify` and `save`. The invoices example and tests use `tax_id` and `INV-` numbers.
- New docs site design with the Carioca palette (white first, Rio sea blue, canary yellow, sunset
  orange, palm green), a landing page with the launch video, feature clips, benchmarks and a section
  for AI coding agents, and a cleaner header in both themes.
- Every docs page has *Copy as Markdown*, *Ask Claude* and *Ask ChatGPT* buttons, and its Markdown is
  published next to it (`<page>/index.md`, linked as `text/markdown`).
- PyPI metadata: more keywords and classifiers, status Beta.

## [0.6.1] - 2026-10-14

### Fixed

- `RunResult` could not be copied or pickled (`copy.deepcopy`, `pickle`, task queues that pickle
  results), because the outputs mapping added in 0.6.0 held a lock.
- With the Arrow fast path, `repr(result.outputs)`, `dict(result.outputs)` and
  `{**result.outputs}` could show a pyarrow Table instead of records. Every way of reading outputs
  now gives records.

## [0.6.0] - 2026-10-14

### Added

- Columnar fast path with Apache Arrow (`agentl-etl[arrow]`): `csv.read`, `parquet.read` and
  `@dataframe` steps pass pyarrow Tables to `parquet.write`, `postgres.write` (append and replace,
  via `COPY`) and `@dataframe` steps when every consumer can take one. Same results; `arrow: false`
  turns it off. Benchmarks: CSV to pandas to Parquet 10x faster, Parquet to Postgres 22x faster.
- `parquet.write` connector.
- `chunk_size` on the pipeline: linear chains (a streaming reader, row-wise steps and agent steps,
  a writer that appends) run chunk by chunk with bounded memory. Streaming readers: `csv.read`,
  `sqlite.read`, `postgres.read` (server-side cursor). Streaming writers: `csv.write`, `json.write`
  (`.jsonl`), `sqlite.write`, `postgres.write`. Plugins opt in with `stream_reader`,
  `stream_writer` and `rowwise` from `agentl.registry`. `chunked` benchmark.

- Independent steps run in parallel: a step starts as soon as its inputs are done, up to
  `max_parallel_steps` (default 4) at once. After a failure no new step starts and the running ones
  finish. `parallel steps` benchmark.
- Batch mode for agent steps (`mode: batch`): the step goes through Anthropic Message Batches or the
  OpenAI Batch API, with polling (`batch_poll_seconds`, `batch_timeout_seconds`), resume of a pending
  batch after a stopped run, normal-call retry for requests the batch could not answer, and
  `price.batch` for the discount. Events `batch.submitted`, `batch.resumed`, `batch.ended`.
- Prompt caching for agent steps: the instruction is a stable prefix, marked with `cache_control` on
  Anthropic (on by default) and a `cachePoint` on Bedrock (`options.prompt_cache: true`). Cache hits
  are reported as `cache_read_tokens` (Anthropic, OpenAI, Bedrock) and priced with the new
  `price.cache_read` / `price.cache_write`.
- `records_per_call` on agent steps: several records per request, each item validated on its own;
  bad or missing items fall back to a single-record call.
- `llm_calls` and `cache_read_tokens` in `RunResult` and in each step's spend.
- `agent call shapes` benchmark.
- Reproducible benchmarks (`python -m benchmarks`): import time, CSV, transforms, SQLite, Postgres
  upsert, HTTP fan-out, agent step throughput and cache re-run. Results and method on the new
  Performance docs page; CI runs them on tags and weekly.
- Footprint tests: the core stays at `pyyaml` + `pydantic`, and `import agentl` loads no heavy module.

### Changed

- `sqlite.write` and `postgres.write` add columns that are new to an existing table instead of
  failing on them.

- Steps no longer wait for unrelated steps. Set `max_parallel_steps: 1` to run one step at a time as
  before. `fn:` steps now run in worker threads.
- Spend per step is counted per call instead of from run totals, so it stays right when agent steps
  overlap.
- New dashboard: the pipeline graph is drawn by agentl (no Mermaid, no CDN, works offline), with live
  step status, records, duration and cost on each node; KPIs (duration, steps, records, rejected,
  tokens, cost), spend per step, an events timeline with an issues filter, light and dark themes.
  It follows the newest run until you pick one.
- Faster `import agentl` (about 209 ms to 158 ms): `urllib`, `smtplib`, `ssl` and entry points are
  imported only when used.
- Anthropic and Bedrock requests send the instruction and the record as two content blocks of the
  user message (same text as before), so the instruction can be cached.

### Fixed

- The connector registry loads once under a lock, so steps starting together never see it half
  filled.
- `python -m benchmarks --only` with no matching scenario now says so instead of crashing; the docs
  showed the wrong syntax for it.

## [0.5.1] - 2026-10-14

### Security

- URL fetches (`url:` on readers, `http.get`, `http.post`) refuse non-public addresses (loopback,
  private, link-local/cloud metadata, reserved) and non-http(s) schemes, also after redirects.
  Allow internal hosts with `AGENTL_ALLOW_HOSTS` or `AGENTL_ALLOW_PRIVATE_NETWORK=1` (#65).
- Agent prompts put the record in a delimited `<record>` block marked as untrusted data, with
  angle brackets escaped so it cannot break out (#64).
- Releases ship a CycloneDX SBOM and a build provenance attestation; every GitHub Action is
  pinned by commit SHA; OpenSSF Scorecard runs on main (#62, #63).

### Changed

- Pipelines that fetch from internal hosts (intranet APIs, `localhost` services) now need
  `AGENTL_ALLOW_HOSTS` or `AGENTL_ALLOW_PRIVATE_NETWORK=1`.

## [0.5.0] - 2026-10-13

### Added

- `http.get` / `http.post`: `params` (query string, filled per record) and `paginate`
  (`param`, `start`, `next`, `max_pages`).
- `examples/invoices`: a 253-line legacy ETL next to its 45-line agentl pipeline, tested end to end.
- LLM provider `bedrock` (Amazon Bedrock Converse API, structured output through a forced tool).
  Extra `aws` (#16).
- `aws.lambda`: invoke a Lambda function once or once per record (payload templating, `items`,
  `keep_input`, async `Event`), with function errors surfaced. Extra `aws` (#15).

### Fixed

- Invalid YAML is reported as a clear `SpecError` (with a hint for `str?` inside `{ }`) instead of
  a traceback.
- Tokens spent on answers that fail schema validation (and their retry) now count toward
  `budget`, `cost_usd` and spend per step; before, only successful calls were counted.

## [0.4.0] - 2026-10-13

### Added

- `browser.scrape` (Playwright): login steps, wait for JavaScript content, extract tables, text,
  HTML or links, or click a download and pass the file to `file.read`. Extra `browser` (#14).
- `email.read` (IMAP: messages, bodies and attachments, with `explode` into one record per
  attachment for `file.read`) and `email.mark` (flag or move messages at the end of a pipeline) (#13).
- Notification channels `teams`, `discord`, `telegram`, `email` (SMTP), `pagerduty` (trigger and
  resolve) and `opsgenie`; `webhook` accepts `headers`; `register_channel` for custom channels (#27).
- Observability: `JsonLogs`, `OpenTelemetry` (a trace per run, a span per step, metrics) and
  `Sentry` listeners; CLI flags `--log-format json`, `--otel`, `--sentry`. Listener events now
  also carry `pipeline` and `ts`. Extras `otel`, `sentry` (#28).
- Spend per agent step: tokens and `cost_usd` in `step.succeeded` / `step.failed` events, in
  `RunResult.spend` and in a Spend table on the dashboard (#17).

## [0.3.0] - 2026-10-13

### Added

- `sql.read` and `sql.write` for any SQLAlchemy database (MySQL/MariaDB, SQL Server, Oracle,
  Snowflake, BigQuery, DuckDB, ...) with `append`, `replace` and `upsert`. Extras `sql`, `mysql`,
  `mssql`, `oracle`, `snowflake`, `bigquery`, `duckdb` (#24).
- Shared run store in Postgres: `--db postgresql://...`, `AGENTL_DB` or `open_store(url)`; runs,
  events and the agent cache are shared by every worker (#32).
- `agentl.run()` accepts a YAML path with `vars`, `db` and `raise_on_failure` (raises
  `PipelineFailed`); `RunResult.summary()`. Integrations for Celery, Temporal and Airflow, and a
  "Scheduling and orchestration" guide covering cron, GitHub Actions, Prefect, Dagster and Lambda (#31).
- Remote paths for every reader, `file.read`, `csv.write` and `json.write` through fsspec: S3,
  GCS, Azure, SFTP, FTP, SMB, WebDAV, with globs and `storage_options`. New extras `remote`, `s3`,
  `gcs`, `azure`, `sftp` (#23).
- `budget:` per run (`max_tokens`, `max_usd`) with a `budget.exceeded` event, `price:` per LLM and
  `cost_usd` in run results and events (#17).
- LLM providers `openai`, `ollama` and `openai-compatible` (vLLM, LM Studio, Groq, ...), with strict
  JSON-schema output, retries and token usage. Standard library only, no new dependency (#16).

### Changed

- Pipeline folders are no longer added to `sys.path` permanently; see the `fn:` lookup order in
  Extending. Code that relied on importing a pipeline's modules by bare name after a run must import
  them explicitly.
- `--db` accepts a `postgresql://` URL and defaults to `$AGENTL_DB`, then `.agentl/agentl.db`.

### Fixed

- `fn:` modules next to a pipeline are loaded by file path under a unique name: two pipelines with
  their own `steps.py` no longer share (or silently swap) code in one process, and pipeline folders
  are no longer added to `sys.path` (#43).

## [0.2.0] - 2026-10-13

### Added

- Docs for AI agents: `llms.txt` and `llms-full.txt` built with the site, a "Use with AI agents"
  page with tested prompts, and `AGENTS.md` (#10).
- Guide "Migrate a pandas pipeline" and `examples/sales` (CSV over HTTP to Postgres upsert) (#11).
- `agentl run`/`validate` find `fn:` modules next to the pipeline file and in the working directory.
- `agentl.pandas`: `@dataframe` turns `fn(df) -> DataFrame` into a step, plus `to_df` and
  `to_records`; `agentl-etl[pandas]` extra (#8).
- `postgres.read` and `postgres.write` (`append`, `replace`, `upsert` on `key`), with typed
  auto-created tables and batched writes in one transaction; `agentl-etl[postgres]` extra (#9).
- Readers accept `url:` (HTTP download with `headers` and retries) besides `path:`, and upstream
  records whose `path` is a URL (#7).
- `file.read`: detects the format by extension, expands ZIP and GZ archives safely (path traversal
  rejected, size cap) and reads mixed folders (#22).
- New readers: `txt.read`, `parquet.read`, `doc.read` (via LibreOffice), `xml.read`, `html.read`;
  `excel.read` now also reads legacy `.xls`. New extras: `xls`, `parquet`.

### Changed

- `csv.read` detects the delimiter (`,` `;` tab `|`) when `delimiter` is not set.

### Fixed

- `dedupe`, `filter` and `select` accept a single field name (`on: id`); a string used to be
  split into characters and silently dropped records.
- An agent step with an empty upstream now outputs nothing instead of running once on an empty record.

## [0.1.1] - 2026-10-13

### Changed

- The PyPI distribution is now `agentl-etl` (PyPI treats `agentl` as confusable with an existing
  project). Install with `pip install agentl-etl`; the import (`import agentl`) and the `agentl`
  command are unchanged. 0.1.0 was never published.

## [0.1.0] - 2026-10-13

### Added

- YAML pipeline spec with `${VAR}` interpolation and `extract`/`transform`/`load` sections.
- Runner that executes steps in graph order and emits run, step and item events.
- Agent steps with schema-validated output, per-record cache and rejected items.
- Connectors: CSV, JSON/JSONL, Excel, PDF, Word, HTTP, SQLite, `select`, `filter`, `dedupe`, `join`, `concat`.
- LLM providers: Anthropic and an offline `echo` provider.
- Notifications to Slack, generic webhooks and the console.
- CLI (`run`, `validate`, `diagram`, `runs`, `connectors`, `dashboard`) and a local dashboard.

[Unreleased]: https://github.com/arimayuji/agentl/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/arimayuji/agentl/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/arimayuji/agentl/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/arimayuji/agentl/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/arimayuji/agentl/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/arimayuji/agentl/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/arimayuji/agentl/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/arimayuji/agentl/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/arimayuji/agentl/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/arimayuji/agentl/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/arimayuji/agentl/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/arimayuji/agentl/releases/tag/v0.1.0
