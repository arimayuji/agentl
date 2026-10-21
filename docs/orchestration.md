# Scheduling and orchestration

agentl runs pipelines; it does not schedule them. Plug it into whatever already runs your jobs.
Every option below calls the same entry point:

```python
import agentl

result = agentl.run(
    "pipelines/sales.yaml",
    vars={"DAY": "2026-10-13"},  # override ${VAR}s for this run
    db=".agentl/agentl.db",  # run store
    raise_on_failure=True,  # raise agentl.PipelineFailed so the scheduler retries
)
result.summary()  # JSON-serializable: status, run_id, records per step, tokens, cost_usd
```

Retries are cheap: agent results are cached per record, so a retried run only pays for records that
did not finish. Run history lives in the run store; point every worker to the same one to see all
runs in one dashboard: set `AGENTL_DB=postgresql://...` on every worker
([Run store](cli.md#run-store)).

## cron

```cron
0 6 * * *  cd /srv/etl && DAY=$(date +\%F) .venv/bin/agentl run pipelines/sales.yaml
```

`agentl run` exits with 1 when the run fails, so cron mail and wrappers notice it.

## agentl serve: schedules, webhooks and new files

For teams without a scheduler, `agentl serve` runs pipelines on their own triggers, declared in
the YAML:

```yaml
name: invoices
trigger:
  - cron: "0 7 * * 1-5"              # weekdays 07:00, local time (or timezone: America/Sao_Paulo)
  - webhook: /hooks/invoices          # POST JSON here
    secret: ${INVOICES_HOOK_SECRET}
  - files: "inbox/*.pdf"              # new or changed files; remote globs work too (s3://, gs://)
    every: 1m
steps:
  - id: arrived
    use: trigger                      # what started the run
  - id: pages
    use: file.read
    input: arrived
  # ...
```

```bash
agentl serve pipelines/invoices.yaml pipelines/sales.yaml --host 0.0.0.0 --port 7071
```

| Trigger | The `trigger` step gets |
| --- | --- |
| `cron` | `[{scheduled_at}]`. Five fields, names (`mon`, `jan`), ranges, steps, `@daily`/`@hourly`/`@weekly`/`@monthly`; a time missed while the service was down is not run later |
| `schedule` | `[{scheduled_at, cycle, expected_at}]`, see below |
| `webhook` | the JSON body as records (an object is one record). `secret` accepts `Authorization: Bearer <secret>` or an HMAC-SHA256 of the body in `X-Signature-256` / `X-Hub-Signature-256` (`sha256=<hex>`). Answers 202 once queued |
| `every` (on its own) | `[{started_at}]`. Runs again `every` after the last one started, never two at once; for queue consumers |
| `files` | one record per new or changed file: `path`, `name`, `size`, `modified`. The cursor is kept in the run store, so a restart does not reprocess old files, and files are marked as seen only after a successful run |
| `after: <pipeline>` | `[{pipeline, run_id, status, error}]` of the finished run. `status: succeeded` (default), `failed` or `any` |

### Schedules by publication cycle

Data that comes out on expected days, with a variable delay and holidays, does not fit a cron.
A `schedule:` trigger describes the publication instead, always in an explicit time zone:

```yaml
trigger:
  - schedule:
      tz: America/New_York                    # required
      expect:
        days: [mon, tue]
        at: "16:00"
        calendar: us_federal                  # br_national, uk_england, de_national, ca_federal
        exceptions: holidays.txt              # one extra closed date (YYYY-MM-DD) per line
        if_holiday: next_business_day         # skip (default), previous_business_day, run
      poll:
        cadence: [{ for: 15m, every: 1m }, { for: 45m, every: 5m }]
        stop_when: success_in_cycle           # no more polls once a run of the cycle succeeded
      active: { between: ["2026-09-01", "2026-11-30"] }
      extra_dates: [2026-02-19]               # cycles on top of `expect`
      skip_dates: [2026-10-06]                # expected days with no cycle
```

`expect.nth` and `expect.months` add month rules. "Every second month, first Sunday" is:

```yaml
expect: { days: [sun], nth: [1], months: [jan, mar, may, jul, sep, nov], at: "09:00" }
```

`nth` takes 1 to 5, or -1 for the last such weekday of the month; `months` takes `jan`..`dec`
or 1..12. Holidays still apply after the rule picks a day.

The built-in calendars cover national holidays only: `us_federal` (with the Saturday/Sunday
observed rule), `br_national`, `uk_england` (England and Wales bank holidays, moved off
weekends), `de_national` (nationwide German holidays, not state ones) and `ca_federal` (federal
statutory holidays). Regional holidays and one-off closures go in the `exceptions` file.

Each cycle fires at `at`, then on the cadence until its window (the sum of the `for`s) ends. The
`trigger` step gets `[{scheduled_at, cycle, expected_at}]`. When a window closes with no
successful run, `agentl serve` logs a lateness warning and emits a `schedule.late` event with
`cycle` and `expected_at`, which `notify:` routes like any other event (once per cycle; polls that
come due after the window closed are skipped):

```yaml
notify:
  channels:
    slack: { webhook: "${SLACK_WEBHOOK}" }
  rules:
    - on: schedule.late
      to: [slack]
```

Successful cycles are kept in the run store, so a restart does not poll them again.

```bash
agentl schedule next pipelines/releases.yaml --n 10   # the next firings, cron triggers too
```

`agentl lint` refuses a `cron:` trigger without `timezone:` (AL009): it would follow the server
clock and move with daylight saving. `validate` and `run` still accept it.

Runs of one pipeline never overlap: they queue. Different pipelines run side by side. `GET
/health` lists the pipelines served. A `use: trigger` step outside `agentl serve` returns no
records, so the same file still runs with `agentl run`.

### Chaining pipelines and after-run actions

`on_success`, `on_failure` and `after` (either outcome) list what to do when a run ends: start
another served pipeline with `trigger:`, or POST an event with `emit:`. `when:` keeps an action
only if the run metrics match.

```yaml
name: extract_orders
trigger: [{ cron: "0 6 * * *" }]
steps: [...]
on_success:
  - trigger: process_orders            # its `use: trigger` step gets one record: `with`
    with: { rows: "${outputs.extract}", day: "${trigger.day}" }
  - emit: { url: "${EVENTS_URL}", topic: orders.ready, payload: { run: "${run.id}" }, secret: "${EVENTS_SECRET}" }
    when: all_ok and rows_changed > 0
on_failure:
  - trigger: cleanup
```

- References: `${outputs.<step>}` (that step's records), `${trigger.<field>}` (first record that
  started this run), `${run.id}`, `${run.pipeline}`, `${run.status}`, `${run.error}`.
- `when` reads `outcome` / `status`, `all_ok` (succeeded with nothing rejected), `failed`,
  `rows_changed` (inserted + updated by upserts), `records`, `rejected`, `cost_usd`, combined with
  comparisons, `and`, `or`, `not`.
- `emit` posts `{id, topic, pipeline, run_id, status, payload}`; `id` and the `Idempotency-Key`
  header are `<run_id>:<topic>`, so receivers can drop repeats. `secret` adds `X-Signature-256`.
- A child run gets a `chain.parent` event and its parent a `chain.child` event (with the child's
  status). The dashboard's run view has a Chain card with links to the parent and each child.
- Every chained pipeline must be passed to `agentl serve`; a pipeline only started by others needs
  no `trigger:`. Cycles (`a -> b -> a`) are rejected when the service starts.

Fan out, then act once every child is done, and wait for reference loads:

```yaml
name: plan_loads
trigger: [{ cron: "0 5 * * *" }]
depends_on: [load_reference]           # its last run must have succeeded
steps: [...]
on_success:
  - trigger: load_day                  # one child run per record of the step `days`
    foreach: "${outputs.days}"
    with: { day: "${item.day}" }       # without `with`, the child gets the record itself
after_all:                             # once every foreach child finished, whatever the outcome
  when: all_ok and rows_changed > 0    # over the children taken together
  do:
    - use: email.send                  # `use` calls a connector once with `with`
      with: { to: [desk@example.org], subject: "Loads for ${run.pipeline} done" }
    - emit: { use: sqs.send, with: { queue_url: "${QUEUE_URL}", dedup_field: id }, topic: loads.done }
```

- `foreach` takes a reference to a list of records (`${outputs.<step>}`, `${trigger.<field>}`);
  `${item.<field>}` reads the current one. Each child gets a `chain.parent` event as above.
- `after_all.when` reads the same metrics, combined: `all_ok` and `outcome` only if every child
  succeeded, `rows_changed`, `records`, `rejected` and `cost_usd` summed. The parent run gets a
  `chain.after_all` event with them and the number of children. A fan-out with no records runs
  `after_all` at once.
- `depends_on`: before a run starts, a dependency that is queued or running is waited for; if its
  last run did not succeed (or it never ran under this service and the store has no run of it),
  the run is not started and the service logs why.
- `emit` with `use` instead of `url` sends the event as one record through a queue connector
  (`sqs.send`, `kafka.produce`, `rabbitmq.publish`, `pubsub.publish`, `mqtt.publish`). Key or
  deduplicate by its `id` field (`dedup_field: id` on a FIFO queue, `key: id` on Kafka) to keep it
  idempotent by run.

`agentl serve` is one process. If you already have cron, Airflow or GitHub Actions, keep them:
they bring retries, history and high availability that a single process does not. Put a reverse
proxy with TLS in front of webhooks exposed to the internet.

## Sources with no fixed publishing time

A portal that posts yesterday's file sometime between 6 and 11 am, an API that updates "in the
morning": a fixed cron either runs too early, on stale data, or too late. Schedule the run at the
earliest time and let a `wait_until` step hold the pipeline until the data is there:

```yaml
steps:
  - id: portal
    use: http.get
    with: { url: https://portal.example.com/api/daily }
  - id: wait_report
    wait_until:
      step: portal                       # re-run this step...
      condition: "max(date) == today"    # ...until this holds
      every: 15m
      timeout: 5h
      on_timeout: fail                   # or continue with the last data
  - id: save
    use: postgres.write
    input: wait_report
    with: { dsn: "${PG_DSN}", table: daily_report }

notify:
  channels:
    slack: { webhook: "${SLACK_WEBHOOK}" }
  rules:
    - on: wait.timeout                   # "the portal had not published by 11 am"
      to: [slack]
```

| Condition | Holds when |
| --- | --- |
| `rows > 0` (default) | the source returned at least one record; any of `> >= < <= == !=` with a whole number |
| `max(date) == today` | an aggregate of a field (`max`, `min`, `sum`, `count`) compared with a number, a quoted string, `today` or `yesterday`. Dates compare on their first 10 characters, so `2026-10-14T06:30` counts as today |
| `changed` | the records differ from the last run where this wait succeeded |

The first check uses the source's normal output; each later one re-runs the source step (without
its cache). Each check emits `wait.polled`; then `wait.done`, or `wait.timeout` when the next
check would pass `timeout`. With `on_timeout: fail` the run fails before the data reaches the next
step; with `continue` it goes on with the last data it got. The run process stays alive while it
waits, so give your scheduler's job timeout room for it.

## Fetch windows: revisions and backfills

Time-series APIs revise recent values and cap the range per call. A `window:` section handles both:

```yaml
window:
  lookback: 10d            # every run re-reads the last 10 days (revisions)
  max_span: 31d            # at most 31 days per chunk
  align: calendar_month    # none | calendar_month | week
  order: oldest_first      # or newest_first
steps:
  - id: series
    use: http.get
    with: { url: https://api.example.org/series, params: { from: "${window.start}", to: "${window.end}" } }
```

`${window.start}` and `${window.end}` are ISO dates; the end is exclusive. `agentl run` covers
`[today - lookback, tomorrow)`. `agentl backfill p.yaml --from 2025-01-01 --to 2025-12-31` takes
the same path with more chunks (`--to` is inclusive, as in the params-based backfill below). The pipeline runs once per chunk and each chunk's status is kept in
the run store: running the same backfill again skips the chunks that succeeded (`--redo`, or
its alias `--no-resume`, re-runs them), a failed chunk does not stop the others, and the report lists the pending ones.
Durations are whole days or weeks (`10d`, `2w`). Writers should be idempotent (upsert) because the
tail is re-read on every run.

## Overlapping runs and shared caches

A job scheduled every minute must not overlap the previous run, and an expensive lookup should not
run once per worker. Both work through the run store, so they hold across hosts when the workers
share a Postgres store (on one host, the SQLite file is enough).

```yaml
name: prices
concurrency: { key: "{pipeline}:${PRODUCT}", on_overlap: skip }   # skip | queue | fail
steps:
  - id: catalog
    use: cache
    with:
      key: "catalog:${AREA}"
      ttl: 1d
      single_flight: true       # one computes, the others wait for its result
      do: [ { use: http.get, with: { url: "https://api.example.org/catalog" } } ]
  - id: prices
    input: catalog
    use: http.get
    with: { url: "https://api.example.org/prices" }
```

`concurrency` allows one run at a time per `key` (default `{pipeline}`, the pipeline name). When
another run holds the key:

| `on_overlap` | The new run |
| --- | --- |
| `skip` (default) | ends at once with status `skipped`. Not a failure: `agentl run` exits 0 and `raise_on_failure` does not raise |
| `queue` | waits for the other run to finish, up to `wait` (default `1h`), then fails |
| `fail` | ends with status `failed` |

The lock is a lease (`lease`, default `5m`) that the running process renews while it works. If the
process dies, the lease lapses and the next run takes over; no stale lock to clear by hand.

A `cache` step runs its `do` steps (each fed the output of the one before; the first gets the
step's input) and keeps the result under `key` for `ttl` (`30m`, `6h`, `1d`; default: no expiry).
With `single_flight` (the default), concurrent callers compute it once: threads in one run, runs
in parallel and workers on other hosts. One holds a lease on the key, the others wait up to `wait`
for its result; a holder that dies is replaced after `lease` (default `1m`). `cache_ttl: 6h` on any
cached step (agent steps cache per record by default) makes those entries expire too. To force a
fresh computation, `agentl cache clear catalog:north`.

## GitHub Actions

```yaml
on:
  schedule: [{ cron: "0 6 * * *" }]
jobs:
  etl:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.13" }
      - run: pip install 'agentl-etl[postgres]'
      - run: agentl run pipelines/sales.yaml
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
```

## Run parameters

A pipeline started by a scheduler usually needs an argument: the day to load, the file to read.
Declare them under `params:` and use them as `${params.name}`:

```yaml
name: daily_sales
params:
  day: date                                  # required: no default
  path: {type: str, default: reports/latest.xlsx}
  limit: {type: int, default: 1000, help: max rows per run}
steps:
  - {id: raw, use: excel.read, with: {path: "${params.path}"}}
  - {id: out, use: json.write, input: raw, with: {path: "out/sales-${params.day}.json"}}
```

Types are `str` (the default), `int`, `float`, `bool` and `date` (ISO `YYYY-MM-DD`). A param
with no default is required. A string that is exactly `${params.limit}` keeps its type (an int
stays an int); inside a longer string the value is written as text.

Pass values from any entry point:

```bash
agentl run daily_sales.yaml --param day=2026-10-15 --param path=reports/2026-10-15.xlsx
agentl serve daily_sales.yaml --param day=2026-10-15   # applied to every run it starts
```

```python
import agentl

result = agentl.run("daily_sales.yaml", params={"day": "2026-10-15"})
result.summary()  # {"status": ..., "records": {...}, "rejected": {...}, "cost_usd": ...}
```

Unknown, missing and mistyped values raise `SpecError` (exit code 2 on the CLI) before any step
runs. `agentl lint` checks the declarations and reports `${params.x}` references to names that
are not declared (AE010).

## Backfill

`agentl backfill` reprocesses history: one run per file or per period, each passed as a run
parameter. When the pipeline has a `window:` section the same command backfills by
[fetch windows](#fetch-windows-revisions-and-backfills) instead: only `--from`/`--to` apply.

```bash
# one run per file; s3://, gs://... globs need agentl-etl[remote]
agentl backfill daily_sales.yaml --glob 'reports/**/*.xlsx' --param-name path --param day=2026-10-15 --concurrency 4

# one run per day (or --every week | month)
agentl backfill daily_sales.yaml --from 2026-01-01 --to 2026-03-31 --param-name day
```

- `--param-name` is the param that gets each file or date. Without it, agentl uses the only
  `str` param (files) or the only `date` param (periods), and stops if there is not exactly one.
- Every item is validated before the first run, so a bad date stops the backfill up front.
- After each successful run the item is recorded in the run store (`--db`). Start the same
  command again after an interruption and finished items are skipped; failed ones run again.
  `--redo` runs everything.
- The final report lists succeeded, skipped and failed items (`--json` for a machine-readable
  one); the exit code is 1 if any item failed.

From Python: `agentl.backfill.backfill(path, items, param="day", concurrency=4)` with
`agentl.backfill.periods("2026-01-01", "2026-03-31")` or `agentl.backfill.files("reports/*.xlsx")`.

## Resume a failed run

Running a failed pipeline again reruns every step, and a sink that already wrote writes again.
`agentl resume` picks up where the run stopped instead:

```bash
agentl run orders.yaml            # fails at step "notify", after "save" wrote
agentl resume 20261019-101500-a1b2c3 orders.yaml
```

- While a run executes, each completed step records a checkpoint in the run store: its id and a
  fingerprint of its output. The output itself is spilled to `.agentl/checkpoints/<run_id>/`
  (next to the SQLite store, or `$AGENTL_CHECKPOINT_DIR`) when it is plain JSON and under
  `AGENTL_CHECKPOINT_MAX_BYTES` (8 MB per step by default). The files are deleted when the run
  succeeds; `AGENTL_CHECKPOINTS=0` turns them off.
- Resume works only on a **failed** run of the same pipeline whose spec hash is unchanged: the
  YAML, its `${VAR}` values and `--param` values must be the same. Otherwise it stops with
  `cannot resume` (exit code 2) and records nothing; run the pipeline again instead.
- Completed steps, sinks included, are skipped and their outputs read back (checked against the
  fingerprint). The new run starts from the first failed or pending step; its `run.started`
  event carries `resumed_from`, and a `run.resumed` event lists the skipped and re-run steps.
- When a step that is needed downstream has no stored output, that step runs again and
  `run.resumed` says why. Agent steps hit the per-record cache, so they cost nothing twice. A
  writer (`*.write`, `*.send`, `*.post`...) is never run again: if its output is missing,
  resume refuses.
- Outputs that are never stored, so the step runs again on resume: Arrow tables and values JSON
  cannot hold (dates, bytes), outputs over the size limit or holding a secret value, `unprotect`
  steps, `cursor:` sources (the cursor did not move, so they read the same rows), routers, and
  queue consumers that acknowledge after the run.
- A chunked chain (`chunk_size`) is one unit: it is skipped or run again as a whole.
- With a Postgres store the checkpoints are events, but the files stay on the host that ran the
  step: resume there, or point `AGENTL_CHECKPOINT_DIR` at shared storage.

From Python: `agentl.runner.resume("orders.yaml", run_id)`, or `Runner(spec, resume_from=run_id)`.

### Which writers are safe to run again

Resume never writes a completed sink twice, but a step that failed half way (a sink that wrote
some rows, then crashed) runs again from the start, and a plain re-run repeats every write. Make
sinks idempotent so either is harmless:

| Writer | Run again |
|---|---|
| `mode: upsert` (SQL writers) | Safe: one row per `key`, updated in place |
| `mode: changeset`, `mode: scd2` | Safe: compares with what is there; unchanged rows are left alone |
| `mode: replace`, file writers to a fixed path | Safe: the target is rewritten as a whole |
| `mode: append`, file writers with `{run_id}` in the path | Duplicates rows or files |
| `*.send`, `*.post`, `*.publish` (messages, webhooks, queues) | Delivers again; receivers must drop repeats (`Idempotency-Key`) |

Recommendation: load with `upsert` or `changeset` on a stable key. `fn:` steps with side effects
are not recognized as writers by name; keep them idempotent too.

## Keep originals and replay them

Sources rarely keep history, so reprocessing last month with fixed code means fetching data that
is gone. Mark the step whose output is the original payload with `persist: raw`:

```yaml
raw:
  path: data/raw          # default: $AGENTL_RAW_DIR, else raw/ next to the run store
  keep_days: 90           # default: keep forever
steps:
  - id: download
    use: http.get
    with: { url: "https://example.com/orders.json" }
    persist: raw
  - id: rows
    agent: Extract the order fields.
    input: download
```

- Each run stores the step's output as JSON in `objects/<aa>/<sha256>.json`: addressed by its
  content hash, written once and read-only, never overwritten. The same payload in two runs is
  one file. A manifest in `runs/<run_id>/` catalogues it (pipeline, step, hash, size, records,
  time) and the run gets a `raw.stored` event.
- Outputs JSON cannot hold, outputs holding a secret value, `unprotect` steps and outputs over
  `AGENTL_RAW_MAX_BYTES` (256 MB by default) are not stored: the run gets `raw.skipped` with the
  reason. A `persist: raw` step is never streamed in a chunked chain.
- `raw.keep_days` drops manifests older than that after each run, then every original no
  manifest references (`raw.pruned` event). `agentl raw prune [PIPELINE] --keep-days N` does the
  same by hand.

```bash
agentl raw ls orders.yaml                                 # what is stored, oldest first
agentl replay orders.yaml --from-raw 20261019-101500-a1b2c3
```

`agentl replay --from-raw RUN_ID` runs the **current** pipeline with each `persist: raw` step's
output read back from that run's originals, checked against the hash. The raw steps and the steps
upstream of them do not run, so no source is fetched; everything downstream, sinks included,
runs. Agent steps use the per-record cache, so a replay costs nothing new unless the prompt or
the input changed. The run's `run.started` event carries `replayed_from_raw`. It stops with
`cannot replay` (exit code 2) when the run has no original for a raw step, an original is
missing or changed, or a step downstream reads a step upstream of the raw zone directly.

From Python: `agentl.runner.replay_raw("orders.yaml", run_id)`, or `Runner(spec, raw_from=run_id)`.

## Celery

`pip install 'agentl-etl[celery]'`

```python
from celery import Celery
from agentl.integrations.celery import register

app = Celery("etl", broker="redis://localhost:6379/0")
run_pipeline = register(app)  # task "agentl.run", retries PipelineFailed with backoff (max 3)

run_pipeline.delay("pipelines/sales.yaml", params={"day": "2026-10-13"})
```

The task returns the run summary. A failed run raises `PipelineFailed` and is retried; bad
params raise `SpecError`, which is not retried. Schedule it with Celery beat like any task. Pass task options to `register(app, max_retries=5, ...)`.

## Temporal

`pip install 'agentl-etl[temporal]'`

```python
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from temporalio import workflow
from temporalio.worker import Worker

from agentl.integrations.temporal import RunInput, run_pipeline


@workflow.defn
class DailySales:
    @workflow.run
    async def run(self, day: str) -> dict:
        return await workflow.execute_activity(
            run_pipeline,
            RunInput(pipeline="pipelines/sales.yaml", params={"day": day}),
            start_to_close_timeout=timedelta(hours=1),
        )


worker = Worker(
    client,
    task_queue="etl",
    workflows=[DailySales],
    activities=[run_pipeline],
    activity_executor=ThreadPoolExecutor(4),  # run_pipeline is a sync activity
)
```

## Airflow

`pip install 'agentl-etl[airflow]'` (or add `agentl-etl` to an existing Airflow image)

```python
from agentl.integrations.airflow import AgentlOperator

AgentlOperator(task_id="sales", pipeline="pipelines/sales.yaml", run_params={"day": "{{ ds }}"})
```

`pipeline`, `vars` and `run_params` are templated; the run summary goes to XCom. The argument is
`run_params` because `params` is Airflow's own DAG-level parameters.

## Prefect

```python
import agentl
from prefect import flow, task


@task(retries=3, retry_delay_seconds=60)
def sales(day: str) -> dict:
    return agentl.run("pipelines/sales.yaml", vars={"DAY": day}, raise_on_failure=True).summary()


@flow
def daily(day: str):
    sales(day)
```

## Dagster

```python
import agentl
from dagster import op


@op
def sales(context) -> dict:
    return agentl.run("pipelines/sales.yaml", raise_on_failure=True).summary()
```

## AWS Lambda

```python
import agentl


def handler(event, context):
    result = agentl.run("pipeline.yaml", vars=event.get("vars", {}), db="/tmp/agentl.db")
    return result.summary()
```

Lambda's file system is read-only except `/tmp`, hence `db="/tmp/agentl.db"`. Use remote paths
(`s3://`) for inputs and outputs.
