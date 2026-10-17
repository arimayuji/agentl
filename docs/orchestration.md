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
| `webhook` | the JSON body as records (an object is one record). `secret` accepts `Authorization: Bearer <secret>` or an HMAC-SHA256 of the body in `X-Signature-256` / `X-Hub-Signature-256` (`sha256=<hex>`). Answers 202 once queued |
| `every` (on its own) | `[{started_at}]`. Runs again `every` after the last one started, never two at once; for queue consumers |
| `files` | one record per new or changed file: `path`, `name`, `size`, `modified`. The cursor is kept in the run store, so a restart does not reprocess old files, and files are marked as seen only after a successful run |

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

## Celery

`pip install 'agentl-etl[celery]'`

```python
from celery import Celery
from agentl.integrations.celery import register

app = Celery("etl", broker="redis://localhost:6379/0")
run_pipeline = register(app)  # task "agentl.run", retries PipelineFailed with backoff (max 3)

run_pipeline.delay("pipelines/sales.yaml", vars={"DAY": "2026-10-13"})
```

Schedule it with Celery beat like any task. Pass task options to `register(app, max_retries=5, ...)`.

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
            RunInput(pipeline="pipelines/sales.yaml", vars={"DAY": day}),
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

AgentlOperator(task_id="sales", pipeline="pipelines/sales.yaml", vars={"DAY": "{{ ds }}"})
```

`pipeline` and `vars` are templated; the run summary goes to XCom.

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
