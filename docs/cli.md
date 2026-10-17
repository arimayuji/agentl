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
| AL010 | a `cast` step has no `locale` (so pt-BR) and its numeric fields, read from a local CSV, look like en-US numbers (`1,234.56`, `1.050`) |

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
