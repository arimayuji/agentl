# Concepts

## Everything is a step

A step receives records (`list[dict]`) from its inputs and returns records. Extract, transform and
load are labels, not different machinery.

| Step kind | YAML | Use for |
|---|---|---|
| Connector | `use: pdf.read` | Reading and writing files, APIs and databases |
| Function | `fn: my_module:clean` | Your own deterministic code |
| Agent | `agent: "..."` + `output:` | Semantic work: classify, extract, normalize, audit |
| Router | `agent: "..."` + `routes:` | Send each record to one branch ([routing](agents.md#route-each-record-to-one-branch)) |

## Steps form a graph

`input` points to one step or a list of steps. That makes ETL, ELT, fan-out and joins the same thing:

```yaml
steps:
  - { id: a, use: csv.read, with: { path: a.csv } }
  - { id: b, use: json.read, with: { path: b.json } }
  - { id: ab, use: join, input: [a, b], with: { left: a, right: b, on: id } }
```

The runner executes steps in dependency order and refuses cycles. A step starts as soon as all its
inputs are done, so independent branches run at the same time: above, `a` and `b` are read together
and `ab` starts when both finish.

```yaml
max_parallel_steps: 4   # default; 1 runs one step at a time, in declaration order
```

- Ready steps start in declaration order, up to the limit.
- If a step fails, no new step starts. Steps already running finish, then the run fails with the
  first error.
- Results are the same as running one at a time. Events of each step keep their order; events of
  steps running together interleave in the order they happened.
- Steps run in threads. A `fn:` step that changes shared global state should take a lock, or set
  `max_parallel_steps: 1`.
- Spend and budget are counted per step and for the run, whichever steps overlap.

### Required and best-effort steps

A step is required by default: its failure fails the run. Mark a destination `required: false`
(or `criticality: best_effort`) when its failure should be recorded but not stop anything, for
example the legacy table during a migration. `after:` orders steps without passing data.

```yaml
steps:
  - { id: rows, use: csv.read, with: { path: facts.csv } }
  - { id: new, use: postgres.write, input: rows, with: { table: new_facts } }
  - { id: legacy, use: sql.write, input: rows, with: { table: legacy_facts },
      required: false, after: new }
```

- A failing best-effort step emits `step.failed` with `required: false`; the run keeps going and
  ends `succeeded`. Writes of other steps are not rolled back.
- `RunResult.best_effort_failed` maps each failed best-effort step to its error,
  `RunResult.partial` is true when that is not empty, and `run.succeeded` and `summary()` carry
  `partial` too. Cursors and queue acknowledgements follow `succeeded` as usual.
- No step may take a best-effort step as `input` (its output is empty when it fails); use `after:`
  to run something once it is done. `after:` waits for a best-effort step whether it failed or not.
- Without `after:`, independent destinations still run in parallel.
- A budget stop (`budget.exceeded`) always fails the run.

### Retries and fallbacks

Any `use`, `fn` or agent step can retry on errors that are likely to pass, and a `use` or `fn`
step can fall back to other step bodies with the same input. Both are off by default.

```yaml
- id: prices
  use: http.get
  with: { url: https://api.example.com/prices }
  retries: { attempts: 3, backoff: 2s, max_backoff: 30s, on: [transport, http_5xx, http_429] }
  fallback:
    - use: web.crawl
      with: { url: https://example.com/prices }
    - use: csv.read
      with: { path: cache/prices.csv }
  fallback_tag: true
```

- `retries`: `attempts` (default 3, the first try included), `backoff` (default `1s`, doubled
  after each try, times a jitter factor between 0.5 and 1), `max_backoff` (default `30s`) and `on`,
  the errors that count: `transport` (timeouts and connection errors), `http_5xx`, `http_429` or
  `any`. A `Retry-After` header on the error (seconds or a date) is honored, up to `max_backoff`.
  Other errors fail at once. Each wait emits `step.retry` with `attempt`, `error` and `wait`.
- `fallback`: step bodies `{use, with}` tried in order once the main body (with its retries) has
  failed. The first that succeeds gives the step output; downstream steps see no difference. Each
  switch emits `step.fallback` with `from`, `to` and `error`. When every body fails, the step fails
  with the last error. Fallback bodies are not retried and cannot have fallbacks of their own.
- `fallback_tag: true` adds `_source` to each record: `main` or `fallback[i]` (from 0).
- A run that used a fallback ends with outcome `partial` (status `succeeded`);
  `fallback_outcome: success` keeps it `success`. `RunResult.fallbacks` maps each such step to
  the body that answered.
- A writer (`*.write`, `*.send`, `*.upload`, ...) takes a fallback only with
  `fallback_writer: true`, since a failed write may have written part of the data. Agent steps
  fall back between llms instead: `fallback:` on the [llm](agents.md) or `llm: [cheap, strong]`.
- A budget stop (`budget.exceeded`) and `NoChange` are never retried nor sent to a fallback, and a
  cached result is returned without running the step. Spend of every try counts toward budgets.
- `agentl lint` checks fallback connectors (AE003) and warns (AL011) about an http source with a
  fallback but no `retries`, which would switch on the first transient error.

## Large inputs: chunk_size

By default every step gets and returns all its records in memory. For big files and tables, set
`chunk_size` and the runner streams linear chains a chunk at a time:

```yaml
chunk_size: 50000
steps:
  - { id: read, use: csv.read, with: { path: big.csv } }
  - { id: paid, use: filter, input: read, with: { where: { status: paid } } }
  - { id: tag, agent: Classify the line., input: paid, output: { category: str } }
  - { id: save, use: postgres.write, input: tag, with: { dsn: "${PG_DSN}", table: lines } }
```

- A chain starts at a source that can stream (`csv.read`, `sqlite.read`, `postgres.read` with a
  server-side cursor), goes through steps that work record by record (`select`, `filter`, agent
  steps) and can end in a writer that appends per chunk (`csv.write`, `json.write` for `.jsonl`,
  `sqlite.write`, `postgres.write`), all in one file or transaction.
- Each step in the chain must have that single input and be its only consumer. Anything else is a
  boundary where records are collected as usual: `dedupe`, `join`, `concat`, `fn:` steps, agent
  steps with `mode: batch`, or a step whose output feeds two steps.
- Only the last step of a chain keeps its output in `RunResult.outputs`. The `step.succeeded` event
  of every step still has its record count, plus `chunked: true`.
- Agent steps keep their per-record cache, budget and rejects; `item.rejected` is emitted per chunk.
- Writers take the columns from the first chunk. `sqlite.write` and `postgres.write` add a column
  that appears later (typed from that chunk in Postgres); `csv.write` fails instead of dropping it.
- Without `chunk_size` nothing changes.

Measured: 1M rows (70 MB CSV) to SQLite grows the process by 790 MB all at once and by 61 MB in
50k chunks; 3M rows in 50k chunks stays at 62 MB. See [Performance](performance.md).

## Columnar fast path (Arrow)

Records are Python dicts. With `pip install 'agentl-etl[arrow]'`, steps that can work on columns
pass a pyarrow Table to each other instead, and records are only built for steps that need them:

| Produces a Table | Takes a Table |
| --- | --- |
| `csv.read`, `parquet.read`, `@dataframe` steps | `parquet.write`, `postgres.write` (append and replace, with `COPY`), `@dataframe` steps |

- A step passes a Table only when every step that reads its output can take one; otherwise
  nothing changes for that edge. Cached and chunked steps always use records.
- `csv.read` keeps every column as text, as without Arrow. It uses the normal reader for what
  pyarrow would read differently: several files, an encoding other than UTF-8, a BOM, duplicate
  or ragged columns.
- `RunResult.outputs` still gives records: a Table is converted the first time you read it.
- On by default when pyarrow is installed. `arrow: false` on the pipeline turns it off, and
  `arrow: true` fails the run if pyarrow is missing.

Measured: CSV to `@dataframe` to Parquet, 500k rows, 6.5 s to 0.6 s with the same Parquet file;
Parquet to Postgres, 200k rows, 4.7 s to 0.2 s with the same rows. See [Performance](performance.md).

## Extract, transform and load sections

If you prefer the classic shape, use sections instead of `steps`. Inside `transform` and `load`, a step
without `input` reads from the previous one:

```yaml
extract:
  - { id: raw, use: csv.read, with: { path: in.csv } }
transform:
  - { use: select, with: { fields: [id, total] } }
load:
  - { use: json.write, with: { path: out.json } }
```

## Environment variables

`${VAR}` is replaced from the environment when the file is loaded. `${VAR:-default}` provides a
fallback. A missing variable without a default is an error.

Values that change from run to run (a date, an input file) are better declared as
[run parameters](orchestration.md#run-parameters): `${params.day}`, typed and checked before the
run starts.

## Incremental loading: cursor

A source that should only read what changed since the last run keeps a cursor:

```yaml
- id: invoices
  use: http.get
  with:
    url: https://api.example.com/invoices
    params: { updated_since: "{cursor}" }
  cursor: { field: updated_at, initial: "2026-01-01" }
```

After a run that **succeeds**, the largest `updated_at` the step returned is stored in the run store
(SQLite or the shared Postgres store), per pipeline and step. The next run replaces `{cursor}` in the
step's `with` with it; the first run uses `initial`. A failed run leaves the cursor where it was, so
the same records are read again. A value that is exactly `"{cursor}"` keeps its type (a number stays
a number); inside a longer string it is formatted as text. Works on `use` and `fn` steps; a step
with a cursor is not streamed in chunks. Each move is logged as a `cursor.advanced` event.

```bash
agentl state show [pipeline]               # stored cursors
agentl state reset <pipeline> [--step id]  # next run starts again from initial
```

## Cache and resume

<video class="feature-clip" autoplay muted loop playsinline preload="none" poster="../assets/clips/cache.jpg" aria-label="A second run served from the per-record cache">
  <source src="../assets/clips/cache.webm" type="video/webm">
  <source src="../assets/clips/cache.mp4" type="video/mp4">
</video>

Agent steps cache each record's result in `.agentl/agentl.db`, keyed by the instruction, the schema,
the model and the record. Re-running a pipeline only pays for records that changed or failed.
Set `cache: true` on other steps to cache them too, or run with `--no-cache` to ignore the cache.

The key also holds the version of agentl's built-in prompts, so an upgrade that changes them asks
the model again instead of serving old answers. After you edit a `fn` or change what a step should
produce in a way the key cannot see, set `cache_version` on the step (any string or number) and
change it whenever the old results should no longer be reused:

```yaml
- id: classify
  agent: Classify the ticket
  output: {category: str}
  cache_version: 2  # bump to invalidate this step's cached answers
```

## Run outcomes

Success or failure alone hides what happened: most polling runs find nothing new, and a source that
was unreachable for one run is not a defect. Each run gets an `outcome` next to its `status`:

| outcome | when | `status` | default exit code |
|---|---|---|---|
| `success` | the run finished and read data | `succeeded` | 0 |
| `no_change` | every step without inputs read 0 records | `succeeded` | 0 |
| `partial` | the run finished but records went to the quarantine | `succeeded` | 0 |
| `skipped` | a timeout or connection error, with `on_transport_error: skipped_upstream` | `skipped` | 0 |
| `failed` | any other error, or too many skipped runs in a row | `failed` | 1 |

`status` keeps its values (`succeeded`, `failed`), so code, orchestrators and notification rules
written before outcomes behave as before; the only new status is `skipped`, which appears only when
you opt in. The run ends with the event `run.succeeded`, `run.failed` or `run.skipped`, whose data
carries `outcome` (and `reason` for skips). When the outcome is in `alert_on`, a `run.alert` event
comes right before it: point notification rules at it to be alerted only for what needs a person.

```yaml
outcomes:
  on_transport_error: skipped_upstream  # default: failed. HTTP 4xx/5xx and parsing stay failed
  on_empty: no_change                   # or success
  on_rejected: partial                  # or success
  alert_on: [failed, partial]           # the default
  max_skipped: 3                        # 3 skipped runs in a row fail (null: never)
  exit_codes: {partial: 2, no_change: 0}
```

A skipped run does not move cursors and releases what queue readers took, like a failed run. The
`max_skipped`-th skipped run in a row is recorded as `failed` (and alerts), so an unreachable source
is not ignored forever. `agentl run` exits with `exit_codes[outcome]` (default 1 for `failed`, 0
otherwise); `run(..., raise_on_failure=True)` raises only for `failed`. `agentl runs`, `agentl
stats` and the dashboard show the outcome.

## Events

Every run emits `run.started`, `step.started`, `step.succeeded`, `step.failed`, `step.retry`, `step.fallback`, `item.rejected`, `tool.called`, `check.passed`, `check.failed`,
`wait.polled`, `wait.done`, `wait.timeout`, `ack.failed`, `source.empty`, `run.succeeded`, `run.failed`, `run.skipped` and `run.alert` (see [run outcomes](#run-outcomes)). They feed the run store, the dashboard and
[notifications](notifications.md).
