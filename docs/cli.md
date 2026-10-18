# CLI and dashboard

```text
agentl run PIPELINE [--no-cache] [--no-check] [--show STEP] [--param KEY=VALUE ...]
agentl backfill PIPELINE --glob PATTERN | --from DATE --to DATE [--every day|week|month]
                [--param-name NAME] [--param KEY=VALUE ...] [--concurrency N] [--redo|--no-resume] [--json]
agentl validate PIPELINE [--format text|json|github] [--connect [--llm]]
agentl plan PIPELINE [--json]
agentl preview PIPELINE STEP [-n 5]
agentl skill install|show [--project]
agentl lint PIPELINE [--strict] [--format text|json|github]
agentl schema
agentl keygen                                   # a key for protect / unprotect
agentl diagram PIPELINE
agentl runs [show RUN_ID [--json]]
agentl cost [--since 30d] [--by pipeline,model] [--json]
agentl connectors [NAME]
agentl new --list | --template NAME [-o FILE]   # tested recipes, no LLM
agentl new "DESCRIPTION" [-o FILE] [--provider P] [--model M]
agentl doctor [RUN_ID] [--suggest] [--pipeline FILE]
agentl stats [PIPELINE] [--since 30d] [--json]
agentl serve PIPELINE... [--host 127.0.0.1] [--port 7071] [--param KEY=VALUE ...]
agentl schedule next PIPELINE [--n 10]
agentl mcp [--root DIR] [--allow-code]   # serve over stdio
agentl mcp install|uninstall --client CLIENT [--project] [--uvx]
agentl mcp status
agentl dashboard [--host 127.0.0.1] [--port 7070]
```

Every command accepts `--db` to choose the run store: a SQLite path or a `postgresql://` URL
(default: `$AGENTL_DB`, then `.agentl/agentl.db`).

Run parameters and `agentl backfill` are described in
[Scheduling and orchestration](orchestration.md#run-parameters).

## Run ledger and LLM cost

`agentl runs show RUN_ID` prints what one run did: records per step, rows changed (`inserted` plus
`updated` from upserts), rejected records, LLM calls, tokens and cost per step with the models that
were paid for, duration and outcome. `--json` prints the same as JSON.

`agentl cost` groups LLM calls, tokens and cost over the runs of a period. `--by` takes any of
`pipeline`, `step`, `model` and `day` (default `pipeline,model`):

```text
$ agentl cost --since 7d --by pipeline,model
tickets  claude-haiku-4-5  420 calls in 7 runs, 210000 in / 21000 out tokens, $0.3150
total $0.3150
```

Both read the runs and events the store already keeps. Steps recorded before per-model spend
existed count under model `?`.

## Validate and lint

`agentl validate` reports every error at once, each with file, line, column, a code and the fix:

```text
$ agentl validate tickets.yaml
tickets.yaml:7:10  error AE003  step 'read': unknown connector 'csv.reed'. Did you mean 'csv.read'?
    fix: use: csv.read
tickets.yaml:10:12  error AE004  step 'classify': input 'raed' does not exist. Did you mean 'read'? Steps: read, classify, save
    fix: input: read
tickets.yaml:25:19  error AE008  environment variable PG_DSN is not set
    fix: export PG_DSN=... (or write ${PG_DSN:-default})
3 error(s)
```

Environment values and secret references are never read or printed: only whether a `${VAR}` is
set. Exit code 2 when there are errors.

`agentl lint` adds best-practice warnings:

| Code | Warns when |
|---|---|
| AL001 | agent steps call a paid LLM and the run has no `budget` |
| AL002 | a step delivers data (database, Sheets, Drive, SharePoint, queues, `http.post`, remote files) with no `check` before it |
| AL003 | a value looks like a secret (API keys, tokens, a password in a DSN) instead of `${VAR}` |
| AL004 | a step produces records no other step uses |
| AL005 | an `fn:` module is neither next to the pipeline nor installed |
| AL006 | a step keeps a `cursor` but has no `{cursor}` in its `with` |
| AL007 | a top-level field is unknown, so it is ignored when the pipeline runs (`budjet:`) |
| AL008 | an agent step runs before a `protect` step, so the LLM provider sees the fields it protects |
| AL009 | a `cron` trigger has no `timezone` (an error under `lint`: the schedule would follow the server clock) |
| AL010 | a `cast` step has no `locale` (so pt-BR) and its numeric fields, read from a local CSV, look like en-US numbers (`1,234.56`, `1.050`) |
| AL011 | an `http.*` source has a `fallback` but no `retries`, so it switches on the first transient error |

Turn rules off for a pipeline with `lint: { disable: [AL004] }`, or for one line with a
`# agentl: disable=AL003` comment. `--strict` fails (exit 1) on warnings too.

`--format json` is for scripts and AI agents (each finding has `code`, `level`, `message`,
`line`, `col`, `fix`); `--format github` prints GitHub Actions annotations, so findings show up on
the pull request:

```yaml
- run: agentl lint pipelines/*.yaml --strict --format github
```

The MCP server's `validate` tool returns the same findings.

### Reach everything before the first run: --connect

`agentl validate --connect` resolves environment variables and secret references, then touches
each external system once without reading or writing data: an HTTP request to each URL (401/403
fail as refused credentials), `select 1` on each database, a lookup for each local input file,
and the key of each LLM in use. `--llm` also makes one tiny test call per LLM.

```text
$ agentl validate triage.yaml --connect
ok: triage (4 steps)
  ok   configuration: environment variables and secret references resolve
  ok   csv.read tickets.csv: 1 file(s)
  FAIL postgres.write postgresql://u:***@db:5432/x: OperationalError: connection refused
  ok   llm cheap (openai gpt-4o-mini): configured (OPENAI_API_KEY set)
  FAIL llm strong (anthropic claude-sonnet-4-5): ANTHROPIC_API_KEY is not set
```

Passwords in connection strings are masked. Remote files (`s3://`, ...) are not checked yet.

### What a run would do and cost: agentl plan

`agentl plan` reads the sources and runs the steps that cannot change anything (readers and
transforms), then, for each agent step, counts the records already in the cache and estimates calls,
tokens and cost. Nothing is written, no LLM is called, no message is taken from a queue and no
`fn:` step runs; steps after one of those are shown as not known.

```text
$ agentl plan triage.yaml
plan for triage (nothing is written, no LLM is called)

  tickets            use    read            40 rec
  triage             agent  estimated       40 rec   cache 0, calls 40, ~7,058-10,789 tokens, $0.0019-$0.0676   high: every record also goes to the last llm
  save               use    writes          40 rec   would write to triage

total: 40 LLM call(s), $0.0019-$0.0676
```

Costs are a range: the length of each answer is unknown until the model writes it (about 4
characters per token, output between short and long answers), and with a cascade the high end
assumes every record also goes to the last llm. An llm with no `price` shows "cost unknown".
`--json` for scripts.

### Autocomplete in the editor

`agentl schema` prints the JSON Schema of a pipeline file. With the VS Code YAML extension:

```bash
agentl schema > pipeline.schema.json
```

```yaml
# yaml-language-server: $schema=./pipeline.schema.json
name: tickets
```

## Snapshots

`agentl snapshot` runs a pipeline and compares each step's output with the JSON saved in
`snapshots/<pipeline name>/<step>.json`. Records are matched by `--key`, not by order, and a change
is shown field by field:

```bash
agentl snapshot pipeline.yaml --key id             # exit 1 when a step differs or has no snapshot
agentl snapshot pipeline.yaml --key id -u          # show the diff and ask before accepting
agentl snapshot pipeline.yaml --step clean --key id,date -u --yes   # accept without asking (CI)
```

```text
clean: ~ [42]
    total: 10.5 -> 12.0
clean: + {"id": 43, "total": 3}
clean: - {"id": 7, "total": 1}
```

Without `--key` a record is either identical or added/removed. Snapshots are written sorted by key
so their diffs in git stay small. Without a terminal, `-u` needs `--yes`.

## Test against a golden file

`agentl test` runs a pipeline on fixture files and compares what each writer would write with an
expected JSON file. Nothing leaves the machine: source steps read the fixtures, every LLM uses the
offline `echo` provider, writers (and `rejected_to`) become pass-through steps, notifications and
budgets are off and the run uses a throwaway store.

```bash
agentl test orders.yaml --fixture tests/stubs/orders.csv --expect tests/golden/orders.json --update
agentl test orders.yaml --fixture tests/stubs/orders.csv --expect tests/golden/orders.json
```

- `--fixture PATH` when the pipeline has one source step, `--fixture STEP=PATH` otherwise (repeat
  it). The file is read with `file.read`, so CSV, JSON, Excel, Parquet and the other formats work.
  Sources that read local files may run without a fixture; any other source needs one.
- The golden file is a list of records (one writer) or `{"writer_step": [records]}`. With no writer,
  the last step is compared. `--update` writes it from the current run.
- Rows are matched by position, or by a field with `--key id`. `_file` and `_archive` are left out;
  `--ignore FIELD` leaves out more.
- Exit code 0 when everything matches, 1 on a difference or a failed run, 2 on a usage error. The
  diff names each changed field:

```text
FAIL save: expected 3 record(s), got 2
  ~ row 1
      amount: expected 31, got 30
  - missing row 2: {"id": 9, "amount": 99}
```

In GitHub Actions:

```yaml
name: pipelines
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install agentl-etl
      - run: agentl test orders.yaml --fixture tests/stubs/orders.csv --expect tests/golden/orders.json
```

Agent steps get `echo` answers (fields copied from the record), so the golden file checks the
pipeline's plumbing, not the model. A dry diff of inserts and updates against the destination
(`agentl diff`) is planned.

## Run store

The run store keeps run history, events and the per-record agent cache.

- **SQLite** (default): one local file, nothing to set up.
- **Postgres**: one store shared by every worker, so the dashboard shows all runs and a record
  processed by one worker is not paid for again by another. Needs `agentl-etl[postgres]`.

```bash
export AGENTL_DB=postgresql://etl:${PGPASSWORD}@db.internal:5432/etl
agentl run pipelines/sales.yaml      # any machine, any worker
agentl dashboard                     # sees every run
```

In Python: `agentl.run("pipeline.yaml", db="postgresql://...")` or `agentl.store.open_store(url)`.
Postgres tables are prefixed (`agentl_runs`, `agentl_events`, `agentl_cache`), so the store can live
in an existing database. They are created on first use; the user needs `CREATE` on the schema.

Cached results can be dropped by key, by prefix or all at once:

```bash
agentl cache clear catalog:north     # one key (a `cache` step's `key`)
agentl cache clear 'catalog:*'       # every key starting with catalog:
agentl cache clear --all             # everything, agent answers included
```

## Seen files

`skip_seen` lets a pipeline run by hand or by a scheduler handle each file once: it drops the
records whose key an earlier successful run already let through. Keys are stored in the run store,
per scope, and marked only when the whole run succeeds, so a failed run handles the same files
again next time.

```yaml
- id: new_files
  use: skip_seen
  input: attachments
  with: { key: sha256, scope: weekly-reports }   # key: sha256 | path | a field
```

```bash
agentl seen list [--scope weekly-reports]
agentl seen forget --scope weekly-reports [KEY]  # reprocess one key, or the whole scope
```

## Column mappings

`map_schema` steps keep one column mapping per input layout in the run store
([how](connectors.md#map-messy-columns-once-map_schema)).

```bash
agentl mappings show [pipeline]            # every stored mapping, as YAML ready to pin
agentl mappings approve sales mapped       # unblock a step with review: required
agentl mappings forget sales mapped        # infer again on the next run (--layout SIG for one)
```

## Rejected records

Records a step rejects (an agent answer that does not match the `output` schema, a value `cast`
cannot read, an expression `compute` cannot evaluate) do not stop the run. Besides the
`item.rejected` event, each one is kept in the run store with the run id, the pipeline, the step,
the reason (`_error`) and the whole record as the step received it (`cast` adds the bad values
under `_raw`). Postgres keeps them in `agentl_rejected`.

```bash
agentl rejected list                              # pending ones, every pipeline
agentl rejected list --pipeline invoices --step classify
agentl rejected show 42                           # one record, as JSON
agentl rejected retry invoices.yaml --step classify
```

`retry` starts a new run with only the pending rejected records of one step (`--step` is needed
when several steps have some). They go through that step and every step downstream of it; steps
that only feed other branches do not run, and a join input from another branch runs again. Records
that passed before are not sent again, and agent steps keep their per-record cache, so nothing is
paid twice. Fix the cause first (the prompt, the schema, the `cast` type, the source); retried
records are stripped of `_error` and `_raw`. When the retry run succeeds they are marked with its
run id (`list --all` shows them); records rejected again are kept under the new run. In Python:
`agentl.runner.retry_rejected(spec, store, step="classify")`.

!!! warning "Privacy"
    Rejected records hold raw data, personal data included, and stay in the store until you delete
    them. Secret values are masked with `***` as in step samples (cloud secret references and
    environment variables with secret-looking names), which also means a retried record carries
    `***` where a secret was. Set `AGENTL_QUARANTINE=0` to keep no rejected records in the store;
    `result.rejected` and the `item.rejected` event still report them.

`validate` parses the YAML, checks the graph and that every connector exists. It does not connect to
databases or URLs. It does resolve `${VAR}`, so every variable without a default must be set; in CI,
export placeholders (`DATABASE_URL=postgresql://ci@localhost/ci agentl validate ...`).

## New pipeline from a description

```bash
agentl new "read invoices.csv, classify each line by cost center, write to Postgres" -o invoices.yaml
```

`new` sends the description and the list of installed connectors to an LLM, validates the YAML it
answers and sends any validation error back for another try (3 attempts). The file is only written
once it validates, and never over an existing file. The diagram goes to stderr. The model is
`--provider` / `--model`, else `$AGENTL_PROVIDER` / `$AGENTL_MODEL`, else Anthropic with the
default model (needs `ANTHROPIC_API_KEY`). Review the result before running it: it is a first
draft, not a tested pipeline.

## Doctor

```bash
agentl doctor                 # the latest failed run
agentl doctor 20261014-091203-ab12cd --suggest --pipeline invoices.yaml
```

`doctor` reads the run store: the step that failed, its error, check findings, rejected records
and the steps that finished. No LLM is called unless you pass `--suggest`, which adds a suggested
fix (with `--pipeline`, the model also sees the YAML).

## Repair (proposal only)

```bash
agentl repair 20261014-091203-ab12cd --pipeline invoices.yaml
agentl repair 20261014-091203-ab12cd --pipeline invoices.yaml --write invoices.fixed.yaml
agentl repair 20261014-091203-ab12cd --pipeline invoices.yaml --llm strong --json
```

`repair` asks an llm for a fix to a failed (or partial) run and answers with a diagnosis, a
confidence, notes and a unified diff against the pipeline YAML. **Nothing is applied**: the diff is
applied in memory only, the result is parsed and linted, and the command reports whether the patched
YAML is valid. An invalid diff goes back to the model once with the validation errors; if the second
answer is still invalid, only the diagnosis is shown. `--write` writes the patched YAML to a new
file and refuses the pipeline path or an existing file. The exit code is 0 for a valid patch, 1
otherwise. The same proposal is the read-only `repair` tool of `agentl mcp`.

What is sent to the llm: what `agentl doctor` reads from the run store (failing step, error, check
findings, rejected-record counts, finished steps), the pipeline YAML as written (`${VAR}`
references, not their values), lint findings, and at most 3 sample records per input of the failing
step, from the store's samples. In those samples, values that look like personal data (emails,
phones, tax ids, cards, IBANs, IPs, names in name-like fields) are replaced by their kind, like
`<email>`, and long values are cut. Secret values (resolved secret references and environment
variables with secret-looking names such as `*_TOKEN`, `*_KEY`, `*_DSN`) are masked as `***` in the
whole payload.

The llm is the pipeline's `default` llm (or `--llm NAME` from its `llm:` section); with a pipeline
that does not load, `$AGENTL_PROVIDER` / `$AGENTL_MODEL`. A repair costs one or two calls with the
pipeline YAML and the context as input; the calls, tokens and cost (from the llm's `price:`) are
printed, the retry is skipped once the pipeline's `budget:` is reached, and a `repair.proposed`
event with the spend is recorded on the run.

## Track record

```bash
agentl stats                     # every pipeline, last 30 days
agentl stats daily-sales --since 90d
agentl stats --json              # the same report as JSON
```

```text
daily-sales: 42 runs, 38 succeeded, 4 failed (90% success)
  duration: median 41.2s, p95 63.0s
  cost: 1.8400 USD
  last run: 20261014-090012-1a2b3c succeeded
  agent cost (USD):
    [classify] claude-haiku-4-5: 0.000410/record, 0.000432/accepted (9480 of 10000), 3100 cache hits, cache saved 1.8414
  failures by reason:
      3x [load] TimeoutError: db.internal:<n> timed out after <n>s  (last 2026-10-12 09:00, ...)
      1x [verify] check failed: <n> rows, expected at least <n>  (last 2026-10-03 09:00, ...)
  run days, oldest first (. all ok, x some failed): ....x.......x..x.....
```

Failures are grouped by the step that failed first and the error with what changes between runs
taken out (numbers, ids, paths, URLs), so the same timeout on different days counts as one
reason. Pass the run id to `agentl doctor` for the details. The dashboard shows the same numbers
for the last 30 days, and the MCP server has a `pipeline_stats` tool. Everything is computed from
the run store; nothing extra is recorded.

For each agent step and model, `agent cost` divides the step's spend by its records (output plus
rejected), by its accepted answers (records not rejected) and by the records an LLM answered.
`cache saved` prices the step's cache hits at that last number. In `--json` these are the
`agents` rows of each pipeline: `cost_per_record`, `cost_per_accepted`, `cost_per_answer`,
`cache_hits` and `cache_saved_usd`. Runs recorded by earlier versions have no model or cache hits in
their events and show up as model `unknown`.

## Eval

`agentl eval pipeline.yaml [STEP] [--compare model=NAME] [--json]` scores agent steps on their
golden sets and exits 1 below `min_accuracy`. See [golden sets](agents.md#golden-sets-test-a-prompt-or-model-change).

## Gaps

A freshness check says the latest data is late; it does not say a day in the middle is missing.
Declare the partitions the destination should hold under a top-level `expect:` block and
`agentl gaps` lists the missing ones:

```yaml
expect:
  partitions:
    every: 1d              # or 1h
    from: "2025-01-01"
    skip: weekends         # weekends, weekdays or ISO dates; a list is fine
    field: ref_date        # the column holding the partition value
  present_in:
    use: postgres.read     # any reader: sqlite.read, sql.read (SQLAlchemy)...
    with: { dsn: "${DATABASE_URL}", query: "select distinct ref_date from facts" }
```

Files instead of a table: `present_in: { files: "out/{date:%Y-%m-%d}/*.parquet" }` is formatted
once per expected period (relative to the YAML file) and the period is present when the glob
matches anything. Composite partitions add `keys: { source: [web, store] }`: every key combination
is expected for every period; the reader rows need a `source` column, and the glob can use
`{source}`.

```bash
agentl gaps pipeline.yaml                         # 3 of 290 partitions missing, as ranges
agentl gaps pipeline.yaml --since 2025-06-01 --until 2025-06-30
agentl gaps pipeline.yaml --json                  # missing periods, ranges and commands
agentl gaps pipeline.yaml --commands              # also print `agentl backfill` lines
agentl gaps pipeline.yaml --fail                  # exit 1 when something is missing
```

`to:` defaults to the last complete period (yesterday, or the previous hour), in UTC. Values read
from the destination are normalised (dates, datetimes and ISO strings) to `format:`, by default
`%Y-%m-%d` or `%Y-%m-%dT%H`.

`--commands` prints one `agentl backfill pipeline.yaml --from A --to B` line per contiguous run of
missing days (with `--param key=value` for composite keys), for the `agentl backfill` command and
run params of [#182](https://github.com/arimayuji/agentl/issues/182). Hourly gaps and custom
formats get no lines. Running only the missing periods automatically (`--fill`, within request
budgets) is not part of this release; for a scheduled check, run `agentl gaps --fail` from cron or
CI and alert on the exit code.

## Diagram

`agentl diagram` prints the pipeline as Mermaid. Connectors are boxes, functions are rounded and
agents are hexagons:

```mermaid
flowchart LR
  invoices["invoices"] --> matched("matched")
  customers["customers"] --> matched
  matched --> classify{{"classify"}}
  classify --> save["save"]
```

## Dashboard

<video class="feature-clip" autoplay muted loop playsinline preload="none" poster="../assets/clips/dashboard.jpg" aria-label="The dashboard during a live run: steps light up, KPIs and spend update, events stream in">
  <source src="../assets/clips/dashboard.webm" type="video/webm">
  <source src="../assets/clips/dashboard.mp4" type="video/mp4">
</video>

<small>Screen recording of `agentl dashboard` during a run (offline demo pipeline with a fake LLM).</small>

`agentl dashboard` serves a local page, refreshed every second: recent runs, the pipeline graph with
live status, records, duration and cost per step, KPIs, spend per step and the event timeline. It is
self-contained (no CDN) and follows the newest run until you pick one. It binds to `127.0.0.1` by default. Do not expose it publicly: it has no authentication.
