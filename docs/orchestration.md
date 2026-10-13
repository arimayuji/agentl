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
runs in one dashboard (a shared Postgres store is tracked in
[#32](https://github.com/arimayuji/agentl/issues/32)).

## cron

```cron
0 6 * * *  cd /srv/etl && DAY=$(date +\%F) .venv/bin/agentl run pipelines/sales.yaml
```

`agentl run` exits with 1 when the run fails, so cron mail and wrappers notice it.

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
