# CLI and dashboard

```text
agentl run PIPELINE [--no-cache] [--no-check] [--show STEP] [--param KEY=VALUE ...]
agentl resume RUN_ID PIPELINE [--no-cache] [--param KEY=VALUE ...]   # skip completed steps
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
agentl cost [--since 30d] [--by pipeline,model] [--usd-per-hour X | --lambda-memory-mb MB | --bill-usd X] [--json]
agentl connectors [NAME]
agentl new --list | --template NAME [-o FILE]   # tested recipes, no LLM
agentl new "DESCRIPTION" [-o FILE] [--provider P] [--model M]
agentl doctor [RUN_ID] [--suggest] [--pipeline FILE]
agentl stats [PIPELINE] [--since 30d] [--label KEY=VALUE ...] [--json]
agentl serve PIPELINE... [--host 127.0.0.1] [--port 7071] [--param KEY=VALUE ...]
agentl schedule next PIPELINE [--n 10]
agentl mcp [--root DIR] [--allow-code]   # serve over stdio
agentl mcp --http [HOST]:PORT [--token-env AGENTL_MCP_TOKEN] [--root DIR]   # streamable HTTP at /mcp
agentl mcp install|uninstall --client CLIENT [--project] [--uvx]
agentl mcp status
agentl dashboard [--host 127.0.0.1] [--port 7070]
agentl dashboard export OUT_DIR [--since 30d] [--label KEY=VALUE ...] [--max-runs 200]
agentl runs [--label KEY=VALUE ...]
```

Every command accepts `--db` to choose the run store: a SQLite path or a `postgresql://` URL
(default: `$AGENTL_DB`, then `.agentl/agentl.db`).

Run parameters and `agentl backfill` are described in
[Scheduling and orchestration](orchestration.md#run-parameters).
`agentl resume` is described in [Resume a failed run](orchestration.md#resume-a-failed-run).

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

### Compute cost per pipeline

One Lambda function or one EC2, ECS or k8s host usually runs many pipelines, so the cloud bill
shows a single line. `agentl cost` can attribute that compute to pipelines, steps and models from
the wall time each run recorded. Pick one price:

| Flag | Formula per run |
| --- | --- |
| `--usd-per-hour X` | `wall_seconds / 3600 * X` (instance, task or pod price) |
| `--lambda-memory-mb MB` | `wall_seconds * MB / 1024 * usd_per_gb_second + usd_per_request` |
| `--bill-usd X` | `X * wall_seconds / sum(wall_seconds of all runs in the period)` |

Lambda prices default to 0.0000166667 USD per GB-second and 0.0000002 USD per request (x86,
us-east-1); override them with `--usd-per-gb-second` and `--usd-per-request`. `--bill-usd` spreads
a known bill for a shared host (for example the month's EC2 line) over the runs of the period.

A run's compute goes to its finished steps in proportion to their `seconds`, and a step's share to
its models in proportion to their LLM calls. Steps without LLM calls show model `-`; a run with no
finished step keeps its compute under step `-`. Each group then has `compute_usd` and `total_usd`
(LLM plus compute), and the report has `llm_usd`, `compute_usd` and `total_usd`:

```text
$ agentl cost --since 30d --by pipeline --usd-per-hour 0.0832
orders   0 calls in 720 runs, 0 in / 0 out tokens, $0.0000 + compute $0.4992 = $0.4992
tickets  420 calls in 30 runs, 210000 in / 21000 out tokens, $0.3150 + compute $0.0693 = $0.3843
total $0.8835 (LLM $0.3150, compute $0.5685, hourly)
```

Limits: the split uses wall time only (not CPU seconds or memory), every run in the store is
priced the same way (run `agentl cost` against one host's store, or one `--db`, per price), and the
host's idle time is not reported; with `--bill-usd` it is spread over the runs.

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
| AL012 | a field of a local CSV source looks like personal data in its sample and reaches an agent step with no `pii:` setting and no `protect` step for it |

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

When the run store has runs of the step with the same model in the last 90 days, the estimate
uses the tokens a record really took in them (input and output, per model paid for, so
escalations and fallbacks are priced at their own model): the low and high are the cheapest and
the dearest run's average, and the line ends with `(history: N records in M runs)`. Otherwise it
is a guess, marked `(guess)`: the length of each answer is unknown until the model writes it
(about 4 characters per token, output between short and long answers), and with a cascade the
high end assumes every record also goes to the last llm. `basis` in `--json` says which. An llm with no `price` shows "cost unknown".
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
pipeline's plumbing, not the model. To see what a run would change in the destination, use
`agentl diff`.

## What a run would change: agentl diff

`agentl diff` runs the pipeline without writing and shows, for each database writer, the rows it
would insert, update and delete:

```text
$ agentl diff orders.yaml
diff for orders (nothing is written)

  save           sqlite.write    out: +1 inserted, ~1 updated, -0 deleted, 1 unchanged
      + {"id": 3, "amount": 30}
      ~ {"id": 2, "amount": 21}
          amount: 20 -> 21
  dump           json.write      3 record(s), not a database writer; not written
```

- `sqlite.write`, `postgres.write` and `sql.write` with a `key` are compared by key with the
  table: each runs as `mode: changeset` with `dry_run: true` and `emit: changes`, so the table is
  read and never changed. `mode: replace` reports the rows the table would lose as deleted;
  `upsert` and `append` never delete; a `changeset` keeps its own `deletes` and `partition_by`.
- Writers without `key`, `mode: scd2` and every other writer (files, HTTP, queues) are not run;
  the line only says how many records would reach them.
- The other steps run as in a real run, `fn:` steps and agent steps included. Agent steps read
  the cache from a copy of the SQLite run store (`--db`) and call the LLM for records not in it;
  `--echo` answers them with the offline `echo` provider instead. The real run store records
  nothing; a Postgres run store is not copied, so its cache is not used.
- `--fixture [STEP=]PATH` reads a file instead of a source, as in `agentl test`.
- Notifications are off. `--limit N` changes how many changed rows are shown per writer (default
  10), `--json` prints every change for scripts.
- Exit code 0, or 1 when the run fails. With `--exit-code` it is also 1 when any writer would
  change something, which makes a check in CI that the destination is in sync:

```yaml
      - run: agentl diff orders.yaml --exit-code --echo
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

The store records its schema version (`schema_version`, or `agentl_schema_version` on Postgres) and
upgrades itself when a newer agentl opens it, keeping cache, state, seen keys and run history.
Stores created before versioning are upgraded the same way. On Postgres, workers that start
together take an advisory lock, so only one of them migrates; the user needs `ALTER` on the tables.

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

## Report a bug

```bash
agentl report --pipeline invoices.yaml            # the latest failed run
agentl report 20261014-091203-ab12cd --pipeline invoices.yaml -o bug.md
agentl report --crash                              # the last CLI crash
agentl report --json
agentl report --submit                             # ask, then file it with your gh account
```

`report` builds a ready-to-paste bug report: the agentl version, Python and OS, the failing step
with its error and traceback, what `agentl doctor` sees, the pipeline YAML and a minimal repro
suggestion. It prints markdown that matches the Bug report issue form, and a prefilled
`https://github.com/arimayuji/agentl/issues/new?template=bug_report.yml&...` URL; when the URL
would pass 8000 characters it asks you to paste the markdown (or the `-o` file) into the form
instead. **Nothing is sent over the network** unless you pass `--submit`. Read it before you share
it.

Every report carries a fingerprint, `agentl-fp-` plus 12 hex characters, made of the error class,
the innermost agentl frame of the traceback (file and function, no line number) and the agentl
version. The same bug gets the same fingerprint, so it can be found among existing issues.

`--submit` prints the report first, then uses your logged-in [GitHub CLI](https://cli.github.com/)
(`gh`): it searches the agentl issues for the fingerprint and, when one matches, lists the matches
and files nothing (add your details there with `gh issue comment`). Otherwise it asks
`File this report as a new issue ... [y/N]` and runs `gh issue create` only on yes. Without `gh`
installed it prints the prefilled URL instead. No project token is used: `gh` acts as you.

In the YAML, `${VAR:-default}` loses its default, values of secret-looking keys (`token`,
`password`, `dsn`, `api_key`...) that are not `${VAR}` references become `***`, and secret values
the process knows (resolved secret references, environment variables with secret-looking names)
are masked everywhere in the report. Hostnames, paths and data values in a traceback are not.

When the CLI crashes with an unexpected Python error, or a run fails with an error that looks
internal (`TypeError`, `AttributeError`... raised outside your own `fn:` code), agentl prints one
line after the error:

```text
Looks like an agentl bug: run `agentl report <run_id>` to file it (silence with AGENTL_NO_REPORT_HINT=1)
```

Per pipeline, `report.on_bug` chooses what happens then:

```yaml
report:
  on_bug: prompt   # the default: print the line above
  # open: also open the prefilled issue form in the browser (nothing is submitted)
  # off: print nothing
```

A CLI crash keeps its traceback in `.agentl/last-crash.txt` for `agentl report --crash`. Set
`AGENTL_NO_REPORT_HINT=1` to never print the line. The same report is the read-only `report` tool
of `agentl mcp`. To fix the bug yourself or with an AI agent, see
[CONTRIBUTING_AI.md](https://github.com/arimayuji/agentl/blob/main/.github/CONTRIBUTING_AI.md).

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
  checks (passed/warn/reject/failed, last):
    [verify] rows: 39/2/0/1, last passed
    [verify] vs_runs: 41/0/0/1, last passed
  failures by reason:
      3x [load] TimeoutError: db.internal:<n> timed out after <n>s  (last 2026-10-12 09:00, ...)
      1x [verify] check failed: <n> rows, expected at least <n>  (last 2026-10-03 09:00, ...)
  run days, oldest first (. all ok, x some failed): ....x.......x..x.....
```

`checks` counts, per check step and rule, how many runs gave each [result](checks.md#severity-per-rule)
and the latest one; `--json` has the same rows under `checks`.

Failures are grouped by the step that failed first and the error with what changes between runs
taken out (numbers, ids, paths, URLs), so the same timeout on different days counts as one
reason. Pass the run id to `agentl doctor` for the details. The dashboard shows the same numbers
for the last 30 days, and the MCP server has a `pipeline_stats` tool. Everything is computed from
the run store; nothing extra is recorded.

For each agent step and model, `agent cost` divides the step's spend by its records (output plus
rejected), by its accepted answers (records not rejected) and by the records an LLM answered.
`cache saved` prices the step's cache hits at that last number. In `--json` these are the
`agents` rows of each pipeline: `cost_per_record`, `cost_per_accepted`, `cost_per_answer`,
`cache_hits` and `cache_saved_usd`.

On a step with [human review](review.md), an `approved` line also counts what people decided: the
accepted answers that went out without review, plus the queued answers a person accepted as
they were. Answers a person fixed or rejected, answers still pending, answers flagged
`_review: pending` and answers that reused a person's answer are not approved. In `--json`:
`approved`, `cost_per_approved` and `review_accepted`, `review_fixed`, `review_rejected` and
`review_pending` (the queued answers of these runs, by status). For the cost per right answer
on known answers, run `agentl eval` on a [golden set](agents.md#golden-sets-test-a-prompt-or-model-change). Runs recorded by earlier versions have no model or cache hits in
their events and show up as model `unknown`.

## Eval

`agentl eval pipeline.yaml [STEP] [--compare model=NAME] [--json]` scores agent steps on their
golden sets, reports the cost per right answer and exits 1 below `min_accuracy`. See [golden sets](agents.md#golden-sets-test-a-prompt-or-model-change).

## Digest

`agentl digest pipeline.yaml [--force] [--show] [--errors]` sends the pipeline's
[`notify.digest`](checks.md#scheduled-digest) when it is due and not yet sent this period, and
prints one line per channel. Run it from cron any time after `at`; it exits 1 when a channel
failed. `--force` sends now, `--show` prints the digest without sending. `--errors` keeps only
the [run errors](notifications.md#errors-in-the-digest) section, with its own sent-today guard.

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
agentl gaps pipeline.yaml --fill --max 30         # backfill only what is missing, 30 runs at most
agentl gaps pipeline.yaml --notify ops            # send the gaps to a notify: channel
```

`to:` defaults to the last complete period (yesterday, or the previous hour), in UTC. Values read
from the destination are normalised (dates, datetimes and ISO strings) to `format:`, by default
`%Y-%m-%d` or `%Y-%m-%dT%H`.

`--commands` prints one `agentl backfill pipeline.yaml --from A --to B` line per contiguous run of
missing days (with `--param key=value` for composite keys), for the `agentl backfill` command and
run params of [#182](https://github.com/arimayuji/agentl/issues/182). Hourly gaps and custom
formats get no lines.

`--fill` runs the pipeline once per missing partition through the same code as `agentl backfill`,
so it is resumable: partitions finished in an earlier call are recorded in the run store and
skipped. The period goes to the pipeline's only `date` param (hourly or custom `format:`
partitions: its only `str` param that is not a composite key); pass `--param-name` when there are
several. Composite keys are passed as params of the same name, so declare them under `params:`.
Other `--param key=value` pairs are passed to every run. `--max N` is the run budget for one call:
the oldest N missing partitions run and the rest are reported as deferred (with `--fail`, exit 1
while some are deferred). `--concurrency` sets the runs at a time. Shared per-hour request budgets
across runs are not part of this release (#212).

Required params that `--param` does not give get a placeholder while `expect:` is read, so a
pipeline with a required `day: date` param can be checked without passing one; `expect:` itself
should not use params.

For a periodic check, run `agentl gaps --notify ops` (or `--fill --notify ops`) from cron, a
scheduler or CI. `--notify` takes a channel declared under the pipeline's `notify.channels`, is
repeatable, and sends the summary (event type `gaps.found`, with the missing partitions as data)
only when something is missing. A failing channel is reported on stderr and does not stop
`--fill`; an unknown channel name exits 2.

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

### Labels and groups

Pipelines that share a store (a SQLite file or Postgres via `--db`) all show up in one dashboard.
Labels organize them. A pipeline can carry a free-form `labels:` map of strings:

```yaml
name: orders_daily
labels: {team: sales-data, domain: orders}
steps: ...
```

Every run also gets two inferred labels, so grouping works with no spec changes: `source` (the
connectors of `use:` steps with no input, e.g. `postgres`) and `sink` (the connectors of `use:`
steps that feed nothing, e.g. `csv`). Several connectors are joined with commas; a spec label with
the same key wins. There is no fixed hierarchy: team, domain and source type are all labels.

The labels are stored with each run. In the dashboard, **Groups** groups pipelines by any label key
(group, then pipelines, then their runs) and shows per group: pipelines, how many are failing (latest
run failed), runs, records written, and spend over 7 and 30 days. The filter box takes
`team=sales-data, domain=orders`; the choice is kept in the URL hash. The same filter works in
`agentl runs --label team=sales-data`, `agentl stats --label ...` and the `labels` argument of the
MCP `pipeline_stats` tool. Filters must all match; `source=http` matches an inferred `source: csv,http`.

### Static export for cloud hosting

```bash
agentl dashboard export site/ --since 30d --label team=sales-data
```

writes `index.html` with the data inlined (it works opened straight from disk), plus JSON
snapshots: `runs.json`, `stats.json`, `groups.json` (per label key) and `runs/<id>.json` for the
latest `--max-runs` runs. No server and no CDN, so any static host serves it: S3, GCS, Cloudflare R2
or Pages, Azure Blob. Refresh it by running the export at the end of a pipeline or on a schedule.

The export holds run metadata, stats and events only, never records, samples or rejected rows.
Event and error text is redacted: keys that look like secrets (`token`, `password`, `api_key`,
`dsn`, ...) become `***`, credentials in URLs, bearer tokens and API-key-shaped strings are masked.
Review what you publish all the same.

!!! warning "Authentication is the host's job"
    The exported site has no login. Keep the bucket private and put access control in front of it:
    Cloudflare Access on Pages or R2, IAM and signed URLs on S3 or GCS, Entra ID on Azure. The
    live `agentl dashboard` server still has no authentication and must not be exposed.

Cloudflare Pages (put Cloudflare Access on the project):

```bash
agentl dashboard export site/
npx wrangler pages deploy site --project-name agentl-dashboard
```

Cloudflare R2 or Amazon S3 (private bucket, served through Access, CloudFront with signed URLs, or
presigned links):

```bash
agentl dashboard export site/
aws s3 sync site/ s3://my-private-bucket/agentl/ --delete
# R2 speaks the S3 API: add --endpoint-url https://<account_id>.r2.cloudflarestorage.com
```

A hosted live dashboard reading a shared Postgres store, with real authentication, is not part of
agentl yet.
