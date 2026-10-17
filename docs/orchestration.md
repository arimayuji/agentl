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
        calendar: us_federal                  # or br_national
        exceptions: holidays.txt              # one extra closed date (YYYY-MM-DD) per line
        if_holiday: next_business_day         # skip (default), previous_business_day, run
      poll:
        cadence: [{ for: 15m, every: 1m }, { for: 45m, every: 5m }]
        stop_when: success_in_cycle           # no more polls once a run of the cycle succeeded
      active: { between: ["2026-09-01", "2026-11-30"] }
      extra_dates: [2026-02-19]               # cycles on top of `expect`
      skip_dates: [2026-10-06]                # expected days with no cycle
```

Each cycle fires at `at`, then on the cadence until its window (the sum of the `for`s) ends. The
`trigger` step gets `[{scheduled_at, cycle, expected_at}]`. When a window closes with no
successful run, `agentl serve` logs a lateness warning (`late: cycle ... had no successful run`).
Successful cycles are kept in the run store, so a restart does not poll them again.

```bash
agentl schedule next pipelines/releases.yaml --n 10   # the next firings, cron triggers too
```

`agentl lint` refuses a `cron:` trigger without `timezone:` (AL009): it would follow the server
clock and move with daylight saving. `validate` and `run` still accept it.

Runs of one pipeline never overlap: they queue. Different pipelines run side by side. `GET
/health` lists the pipelines served. A `use: trigger` step outside `agentl serve` returns no
records, so the same file still runs with `agentl run`.

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
parameter.

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
