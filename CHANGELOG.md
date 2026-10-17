# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version is `0.x`, minor releases may contain breaking changes.
They are always listed under **Changed** or **Removed**.

## [Unreleased]

### Added

- `select` and `cast` say what they do (#276, #277): `select` `fields` accept output names (after
  `rename`) as well as input names, and a listed field no record has fails the step instead of
  becoming nulls; `cast` with `on_bad: fail` stops on a missing or null field unless its type is
  optional (`int?`, `optional: true` or `nullable: true`); lint AL009 warns when a `cast` without
  `locale` (pt-BR) reads a local CSV whose numbers look en-US.
- Agent `attempts:` (#198): per-attempt llm and validation, the validator's structured errors
  (field, rule, value) fed back to the next attempt, and a lenient last attempt that drops invalid
  values to the quarantine. Attempts and tokens per attempt show in events and in the step's spend.
- Run ledger (#209, first part): `agentl runs show RUN_ID` prints records per step, rows changed,
  rejected records, LLM calls, tokens, cost, duration and outcome of one run; `agentl cost --since
  30d --by pipeline,model` groups LLM spend by pipeline, step, model or day. Step events now carry
  `models`: calls, tokens and cost per model, so fallbacks show which model was paid for.
- `time.normalize` step (#213): hour-ending, hour-beginning, half-hour and accumulated-window
  labels to ISO instants with an offset. DST gaps and overlaps fail by default (`on_missing`,
  `on_ambiguous`); `clock: elapsed` reads 23- and 25-hour days.
- `archive.read` (#193): extract zip, tar or gz members whose path matches a regex `pattern`
  (with `password` for ZipCrypto zips), streamed to disk with zip-slip, member count and size
  limits; no match fails listing the members. `csv.read`, `json.read`, `txt.read` and `file.read`
  take `encoding` as a list of attempts or `auto` (UTF-8, then Latin-1) and record the one used in
  `_encoding`.
- `map_schema` step (#177): maps messy columns (multi-row headers, merged cells, varying names)
  onto typed target fields. Matches by name and `synonyms` first, asks the LLM once only for the
  rest, and keeps the mapping per layout in the run store, so later runs make no model call. A new
  layout fails clearly; `review: required` waits for `agentl mappings approve`; `mapping:` pins it
  in the YAML. New `agentl mappings show|approve|forget`.
- `web.crawl` (#240): crawl a site breadth first from `start` URLs and `sitemap` (indexes and
  gzip), with `max_depth`, `max_pages`, `allowed_domains`, `include`/`exclude` regexes and URL
  normalisation and dedupe; one record per page with `url`, `status`, `depth`, `referrer`,
  `links` and `html` and/or `markdown`. Obeys `robots.txt` and `Crawl-delay` by default
  (`obey_robots: false` is logged), waits `delay` per host, honours `Retry-After`, sends an
  identifying User-Agent, takes a `proxy` per run or per host, and with `incremental: true`
  skips pages unchanged since the last successful run (ETag, Last-Modified, content hash).
- `resolve` step (#179): match messy names to a reference table (inline values, a file or a
  previous step). Values are normalised (case, accents, punctuation, suffixes such as Ltda or Inc),
  matched exactly, then by similarity (`difflib`, or `rapidfuzz` with the `fuzzy` extra); only
  values below `threshold` go to the model, once each. Each record gets the match, score and
  method; unresolved ones are rejected. A `resolve.counts` event reports the rows per path, and
  `agentl resolve export|import` round-trips the model decisions for review.
- Hybrid agent steps (#124, #195): `rules:` on an agent step (`compute`, `cast` or `fn`, in order)
  handles each record first; only records where a rule fails or an `output:` field is missing or
  invalid go to the llm. Output keeps the input order, each record carries `_path` (`rules` or
  `agent`), and the `hybrid.paths` event and the step's spend count each path. New `hybrid
  transforms` benchmark compares rules only, agent only and hybrid on messy addresses.
- Run parameters and backfill (#182): declare `params:` in the pipeline (`day: date`, `limit: {type:
  int, default: 100}`; types `str`, `int`, `float`, `bool`, `date`) and use them as `${params.x}`.
  Values come from `agentl run --param key=value`, `agentl.run(..., params={...})`, `agentl serve
  --param` and the Celery, Temporal and Airflow (`run_params=`) integrations; unknown, missing and
  mistyped values fail before the run starts, and `agentl lint` checks declarations and references
  (AE010). `agentl backfill PIPELINE --glob 'reports/**/*.xlsx' | --from DATE --to DATE [--every
  day|week|month] [--concurrency N]` runs once per file or period, records what finished in the
  run store and skips it when started again, and prints a final report.
- Declarative HTML extraction and list-to-detail crawling (#192). `html.select` gives one record
  per element matching `items`, with `fields` read by CSS selector (text, `attr`, `absolute`
  links, `type` with the `cast` types, `all`, `required`), from `url`, `path` or upstream records
  with an `html` field. `crawl` follows `list` links with `next_page` and `max_pages`, runs
  `detail` steps (`html.read`, `html.select`) on each page with `url` and `source_url`, honours
  `limit`, waits `delay` between requests, sends an identifying User-Agent and keeps the
  private-network guard. `html.read` takes `select`, `header_rows` and denormalises `colspan` and
  `rowspan`. `on_selector_miss: fail_with_snapshot` (default) fails naming the selector and URL,
  saves the received HTML and emits `selector.missed`; `skip` and `empty` keep going.
- `sql` step (#121): query the previous steps as tables in an in-process DuckDB (`sql:` on a step,
  or `use: sql` with `query` or `file`). Inputs on the Arrow fast path are scanned without a copy;
  `params` are bound as values; the query cannot touch files, the network or extensions unless
  `allow_files: true`. Errors name the step and suggest step ids; `agentl validate` reports tables
  that are not inputs (AE009). The `duckdb` extra now installs `duckdb` and `pyarrow`, and `duckdb`
  is in `all` and `dev`.
- Schema contracts on any step (#122): `schema:` checks every record a step outputs with the
  `cast` types (`int`, `{money: BRL}`, `{enum: [...]}`, `email?` for optional fields). `on_bad:
  reject` (default) sends bad records to the step's rejected set and the quarantine with the
  reason, `coerce` converts what it can, `fail` stops the run naming the record and field. `extra:
  keep | drop | forbid` for fields outside the schema, `rejected_to: {use, with}` writes the
  rejected records through a writer, and checks take `rejected_rate: "< 1%"`. Unknown types fail
  at parse time with a suggestion. Rejected records of row-wise connectors such as `cast` in
  chunked chains are now kept too.

## [0.11.0] - 2026-10-17

Data cleaning, safe re-runs and IoT: typed `cast`, expressions, reshaping, a quarantine for
rejected records with retry, upserts that report what changed, field-level encryption, and MQTT,
Kinesis and Event Hubs sources with time windows.

### Added

- `kinesis.read` and `eventhubs.consume` (#226): micro-batches from every Kinesis shard or Event
  Hubs partition, with checkpoints (sequence number per shard or partition) kept in the run store
  and advanced only when the run succeeds. Kinesis reads closed parent shards before their
  children. New `eventhubs` extra (`azure-eventhub>=5.11`); Azure IoT Hub works through its Event
  Hubs-compatible endpoint. Connectors can reach their step's stored state with
  `registry.step_state()`.
- `protect` and `unprotect` steps (#225): encrypt (AES-256-GCM, key rotation), keyed hash, mask or
  drop sensitive fields before they reach a destination. `agentl keygen` makes a key, the
  `crypto` extra installs `cryptography`, and `lint` warns (AL008) when an agent step sees the fields
  before `protect`. Decrypted values stay out of the run store's samples.
- `mqtt.subscribe` and `mqtt.publish` (#226), with the new `mqtt` extra (`paho-mqtt>=2.0`).
  Micro-batches with `max_records` / `max_seconds`, TLS with `mqtts://`, `+` and `#` wildcards.
  With a `client_id` the session is persistent and QoS 1/2 messages are acked only when the run
  succeeds, so the broker delivers them again after a failure.
- `aggregate` takes `window: {field, every, end}` (#226): tumbling time windows (`30s`, `5m`,
  `1h`, `1d`) aligned to the epoch, combined with `group_by`, with `window_start` and optional
  `window_end`. Records with an unreadable timestamp are rejected with `_error`.

- Upsert options for `postgres.write` and `sqlite.write` (#183): `on_conflict: update|do_nothing|error`
  and `update_columns`. Upserts report `inserted`, `updated` and `unchanged` in the step result,
  the `step.succeeded` event and the run summary (`writes`); rows whose values did not change are
  not rewritten, so a re-run leaves the table unchanged.
- Quarantine for rejected records (#180): records rejected by agent steps, `cast`, `compute` and
  other connectors are kept in the run store (`rejected` / `agentl_rejected`) with run id, step,
  reason and the raw record, secrets masked. `agentl rejected list|show` lists them and
  `agentl rejected retry PIPELINE --step S` runs only those records through the step and
  downstream, using the agent cache. `AGENTL_QUARANTINE=0` turns storage off.
- `flatten`, `explode`, `pivot` and `unpivot` steps; `join` on several keys (`on: [a, b]` or
  `left_on` / `right_on`), `how: right|outer` and `suffixes`, with an error naming a missing key;
  `dedupe keep: first|last|{latest: field}` (#120).
- `compute`, `aggregate`, `sort`, `limit` and `fill` steps (#120). `compute` evaluates safe
  expressions (`price * qty`, `upper(trim(nome))`, `if(total > 1000, 'alta', 'baixa')`, regex, dates)
  from a closed list of operators and functions, with no way to run code; records it cannot compute
  are rejected with the reason. `aggregate` groups with sum, count, avg, min, max, count_distinct,
  first, last and list.

- `cast` step and semantic types (#119): dates (one day/month order per column, Excel serials,
  "3 out 2026"), datetimes with zones, locale numbers (pt-BR `1.234,56`), money, percent, bool
  (`sim`/`não`), enum with synonyms, text, names, e-mail, CPF and CNPJ (check digits), CEP, UF and
  Brazilian phones (E.164), and for US data `locale: en-US`, `state`, `zip`, US/Canada phones,
  `ein`, `ssn` (format only) and dates such as "Oct 15, 2026". Rows that do not parse go to the step's rejected records with the
  reason and the raw value (`on_bad: reject | fail | keep`). `agentl.types.register_type` adds a
  format. Connectors can now return rejected records the way agent steps do.

- MCP tools for agents (#164): `list_templates` and `new_from_template` (a tested template written
  under the server's root, no LLM call), `describe_connector` (one connector's parameters), and
  `show_output` (the first records a step produced in a run). The runner keeps those first 5
  records per step in the run store, values cut at 200 characters and secret values masked;
  `AGENTL_SAMPLES=0` turns it off. `new_pipeline` is now described as the paid option.

- `agentl mcp install --client claude-desktop|claude-code|cursor|vscode|windsurf [--project]`,
  `agentl mcp uninstall` and `agentl mcp status` (#162): set up the MCP server without editing
  JSON. Other entries are kept, a backup is written first, a second run changes nothing, and a
  file that is not plain JSON is left alone. `python -m agentl` now works too.

- polars steps (#171): `from agentl.polars import dataframe` wraps `fn(df) -> DataFrame` like
  `agentl.pandas.dataframe`, with records or, next to Arrow readers and writers, a pyarrow Table
  wrapped without copying. When every consumer of `csv.read` is a polars step polars reads the
  file, and `parquet.write` after a polars step writes with polars (strings read back as
  `large_string`). New extra `agentl-etl[polars]`. CSV → Parquet comparison: 0.21 s, against
  0.72 s for the pandas step and 0.07 s for plain polars.

- A performance regression gate in CI (#157). Pull requests that touch `agentl/` or `benchmarks/`
  run `python -m benchmarks --compare --against <base checkout>`, which measures the gated jobs
  with the pull request's agentl and the base branch's on the same runner, alternating, and fails
  when time or peak memory grows past a per-job tolerance. `--check` and `--write-baseline`
  compare against a saved file by hand.
- New comparison cases (#157): a paginated API and one request per record against urllib, requests
  and httpx, and an HTML table to CSV against BeautifulSoup and pandas `read_html`. The fan-out and
  HTML cases join the regression gate. `--only` now also filters the `--compare` cases.
- polars in the CSV → Parquet comparison (#157), eager and lazy. It is 10x to 18x faster than
  agentl's `@dataframe` path on that job, and the docs say so. The agentl variants now import
  their libraries before the clock, as the pandas and polars variants already did.

### Fixed

- `--compare` peak memory (#157): it read `ru_maxrss`, which Linux carries over from the parent
  process, so a job's growth often read 0. It now reads `VmHWM`, which starts fresh in each process;
  the published numbers changed (pandas on the CSV job: 574 MB, not 156). The `chunked` scenario
  reads it the same way now (its numbers moved by about 5 MB).

### Changed

- Faster start (#157): the HTTPS opener and its CA store load on the first HTTP call instead of on
  import. A 10-row job's fixed cost went from about 0.14 s to 0.08 s.

- Faster record work (#157): `csv.read` builds records from `csv.reader` instead of `DictReader`,
  `filter` and `select` resolve their fields once per batch instead of once per record, and
  `sqlite.write` checks the columns once per batch. A 1M-row CSV → filter → select → SQLite job went
  from 6.12 s to 3.81 s (pandas 3.90 s, a hand-written loop 3.03 s) with the same output and memory.

## [0.10.0] - 2026-10-16

### Added

- Messaging everywhere: a `.send` step and a `notify:` channel per service, from one sender each
  (#145, #146).
  - Steps: `discord.send`, `slack.send`, `mattermost.send`, `teams.send`, `googlechat.send`,
    `telegram.send`, `whatsapp.send`, `sms.send`, `ntfy.send`, `pushover.send`, `matrix.send` and
    `email.send`.
  - `whatsapp.send` uses the WhatsApp Cloud API, with free text or approved templates. `sms.send`
    uses Twilio, which also reaches WhatsApp.
  - New `notify:` channels: `whatsapp`, `sms`, `googlechat`, `mattermost`, `ntfy`, `pushover`,
    `matrix`.
  - Every step has `{field}` templates, one message per record or packed into few, a
    `max_messages` cap, and retries that honour rate limits. Mentions coming from data are defused.
  - `lint` counts the steps as delivering steps and flags a webhook URL written in the pipeline.
- `agentl new --list` and `agentl new --template NAME [-o FILE]` (#148): the 20 tested recipes ship
  in the package as templates and are written with no LLM call. The templates are copies of
  `examples/recipes` (`tools/sync_templates.py`), checked in CI.
- `agentl connectors <name>` prints a connector's `with:` parameters, types, defaults and notes,
  so an agent writes the step without guessing (#143). `http.get` and `filter` document their
  options.
- `sqlite.write` takes `mode: upsert` with `key`: a record replaces the row with the same key, so
  incremental loads into SQLite need no SQLAlchemy (#143).
- Agent benchmark: three tasks that run twice, where a pipeline should win (#143). They re-run
  with new records, resume after an LLM outage and load incrementally from an API.
  - They run against a local fake LLM that counts calls.
  - New arms: `agentl` (the agent picks a script or a pipeline), `agentl-pipeline` and
    `agentl-python`.
  - The report shows what the second run paid for and tokens without cache reads. An unknown
    `--tasks` name stops the run.
- Promo, terminal and notification videos in `media/launch-video`, with synthesized audio (#142).

### Changed

- `agentl run` and the MCP `run_pipeline` tool check the pipeline first (#147). On an error the
  run does not start, and only the first error is shown with its line and fix. `--no-check`, or
  `no_check` in MCP, skips the check.
- The agentl skill starts from a template and drops the separate lint step. It no longer points to
  web pages an agent may not reach.

## [0.9.0] - 2026-10-15

### Added

- `agentl.read`, `agentl.write` and `agentl.http_get_all`: a short Python API for one-off jobs,
  on the same readers and writers pipelines use (files and globs by extension, URLs, remote
  storage, SQLite without SQLAlchemy, other databases by URL). The skill now picks the size first:
  a script for one-off jobs, a pipeline for jobs that repeat, call an LLM, can fail halfway or need
  checks.
- Agent benchmark: an `agentl-python` arm and a reference solution with the Python API; the local
  API is allowed through agentl's SSRF guard, which had blocked the agentl arms on the API task.

- 20 recipes in `examples/recipes` (PDF invoices to Excel, paginated API to a database,
  incremental loads, web pages to Markdown, emails to fields, cascades, fallbacks, webhooks,
  schedules, alerts...), each run offline in CI, and a Recipes page in the docs.
- An agent benchmark: ten ETL tasks with expected outputs, a harness that gives each to a coding
  agent with and without agentl and scores the output (`python -m benchmarks.agent_bench`), and a
  reference solution that passes all ten in CI. No results with a real agent yet.
- `server.json` for the official MCP registry (`io.github.arimayuji/agentl`), and an `agentl-etl`
  command (same as `agentl`) so `uvx agentl-etl mcp` works without installing.
- `llms-full.txt` includes the code of the examples instead of `--8<--` markers.

- MCP tools `lint`, `plan`, `preview_step` and `new_pipeline`, so an agent can write, check,
  preview and price a pipeline before running it.
- `agentl preview PIPELINE STEP`: the first records into and out of a step, running only readers
  and transforms.
- A Claude Code skill shipped with the package: `agentl skill install [--project]`.
- `agentl validate` reports every error at once with file, line, column, a code and a fix
  ("Did you mean 'csv.read'?"), and unset `${VAR}`s without reading their values.
  `--format json|github`.
- `agentl lint`: best-practice warnings (no budget, delivery with no check, secrets in the YAML,
  unused steps, missing `fn:` modules, unused cursors, unknown top-level fields), with
  `lint: { disable: [...] }`, `# agentl: disable=...` and `--strict`.
- `agentl schema`: JSON Schema of a pipeline file for editor autocomplete.
- `agentl validate --connect [--llm]`: reaches every URL, database, input file and LLM key once,
  without reading or writing data, and masks passwords.
- `agentl plan`: reads sources and runs only safe steps, then shows cache hits, LLM calls, a token
  range and a cost range per agent step, and what would be written. No LLM call, no write.
- The MCP `validate` tool returns the same findings.

### Fixed

- `sql.write` to a SQLite URL and `sqlite.write` create the database's folder when it does not
  exist, as file writers do.

### Changed

- `agentl validate` no longer resolves secret references (`${aws-sm:...}`) or loads the pipeline
  with real values; it only checks that environment variables are set.

## [0.8.1] - 2026-10-15

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
- `join` no longer drops a right-side column that has the same name as a left one: it is kept as
  `<name>_right` (`suffix` to change it).

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

[Unreleased]: https://github.com/arimayuji/agentl/compare/v0.11.0...HEAD
[0.11.0]: https://github.com/arimayuji/agentl/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/arimayuji/agentl/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/arimayuji/agentl/compare/v0.8.1...v0.9.0
[0.8.1]: https://github.com/arimayuji/agentl/compare/v0.8.0...v0.8.1
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
