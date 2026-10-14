# Concepts

## Everything is a step

A step receives records (`list[dict]`) from its inputs and returns records. Extract, transform and
load are labels, not different machinery.

| Step kind | YAML | Use for |
|---|---|---|
| Connector | `use: pdf.read` | Reading and writing files, APIs and databases |
| Function | `fn: my_module:clean` | Your own deterministic code |
| Agent | `agent: "..."` + `output:` | Semantic work: classify, extract, normalize, audit |

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

## Cache and resume

<video class="feature-clip" autoplay muted loop playsinline preload="none" poster="../assets/clips/cache.jpg" aria-label="A second run served from the per-record cache">
  <source src="../assets/clips/cache.webm" type="video/webm">
  <source src="../assets/clips/cache.mp4" type="video/mp4">
</video>

Agent steps cache each record's result in `.agentl/agentl.db`, keyed by the instruction, the schema,
the model and the record. Re-running a pipeline only pays for records that changed or failed.
Set `cache: true` on other steps to cache them too, or run with `--no-cache` to ignore the cache.

## Events

Every run emits `run.started`, `step.started`, `step.succeeded`, `step.failed`, `item.rejected`, `tool.called`,
`source.empty`, `run.succeeded` and `run.failed`. They feed the run store, the dashboard and
[notifications](notifications.md).
