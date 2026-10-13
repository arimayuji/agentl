# Invoices: a legacy ETL and its agentl version

| | Lines |
|---|---|
| [`legacy_etl.py`](legacy_etl.py): requests, retries, pagination, state file, S3, pandas, OpenAI with JSON parsing and retries, SQL upsert, Slack | 253 |
| [`pipeline.yaml`](pipeline.yaml): the same flow in agentl | 45 |
| [`steps.py`](steps.py): the pandas cleaning, reused as a step | 20 |

What moved into agentl: retries and backoff, pagination, the per-record LLM cache (the state file),
schema validation of the model's answers with one retry, rejected records and their rate alert,
batching and upsert, notifications, run history and the dashboard.

```bash
pip install 'agentl-etl[pandas,postgres,s3]'
export VENDOR_API_URL=... VENDOR_API_TOKEN=... DATABASE_URL=... SLACK_WEBHOOK=... OPENAI_API_KEY=...
export RAW_BUCKET=s3://raw-invoices DAY=$(date +%F) SINCE=$(date -d yesterday +%F)
agentl validate examples/invoices/pipeline.yaml
agentl run examples/invoices/pipeline.yaml
```

`tests/test_postgres.py::test_invoices_example_end_to_end` runs this pipeline offline against a
local paginated API, a fake LLM and Postgres.
