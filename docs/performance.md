# Performance

agentl ships a small, reproducible benchmark suite in `benchmarks/`. It uses only local resources:
a temp directory, SQLite, an optional Postgres, a local HTTP server and a fake LLM provider.
No real LLM or external network is called.

```bash
python -m benchmarks            # full run
python -m benchmarks --quick    # 10% of the data, for CI
python -m benchmarks --only csv.read transforms --json out.json
AGENTL_TEST_PG_DSN=postgresql://user@127.0.0.1:5432/db python -m benchmarks   # include Postgres
```

CI runs `--quick` on every tag and weekly, and uploads the JSON as an artifact.

## Results

agentl 0.5.1, Python 3.13, x86_64, 4 CPUs (a shared cloud container). Your numbers will differ:
compare runs on the same machine, not across machines.

| Scenario | Result | What it measures |
| --- | --- | --- |
| `import` | 158 ms over bare Python | `import agentl`, median of 5 fresh processes. No optional module (pandas, requests, boto3, ...) is loaded |
| `csv.read` | 565k records/s | Read 200k rows from CSV (6.6 MB), median of 3 |
| `transforms` | 812k records/s | `filter` + `dedupe` + `select` on 200k records, median of 3 |
| `sqlite.write` | 721k records/s | Write 100k records to SQLite, median of 3 |
| `postgres.upsert` | 75k/s insert, 67k/s update | Upsert 100k records twice (first inserts, then updates) |
| `http.get fan-out` | 461 req/s at concurrency 16 (5.2x vs 1) | 1000 requests to a local server with 10 ms latency |
| `agent step` | 1,471 records/s at concurrency 32 | 500 records, fake provider with 20 ms latency. agentl overhead is about 0.3 ms per record |
| `agent cache re-run` | 31x faster, 0 tokens | Same 500 records again: every call is a cache hit |
| `chunked` | +790 MB all at once, +61 MB with `chunk_size: 50000` (and 2x faster) | 1M rows (70 MB CSV) through `csv.read` → `select` → `sqlite.write`; peak RSS growth of a fresh process |
| `parallel steps` | 1.08 s one at a time, 0.51 s in parallel | Two independent 0.5 s sources and a step on one of them, `max_parallel_steps` 1 vs 4 |
| `agent call shapes` | 0.99 USD default, 0.45 with prompt caching, 0.42 with `records_per_call: 20`, 0.50 in batch mode | 500 records, fake provider with a 400-token fixed prefix (see below) |

## Call shapes

`agent call shapes` runs the same 500 records four ways (concurrency 8, price 3 / 15 / 0.30 per
million input / output / cached input):

| | Calls | Tokens | Cached | Cost (USD) | Seconds |
| --- | --- | --- | --- | --- | --- |
| default | 500 | 250,000 | 0 | 0.99 | 1.3 |
| prompt caching | 500 | 250,000 | 199,600 | 0.45 | 1.3 |
| `records_per_call: 20` | 25 | 60,000 | 0 | 0.42 | 0.1 |
| `mode: batch` (`price.batch: 0.5`) | 500 | 250,000 | 0 | 0.50 | n/a |

The fake provider charges a 400-token prefix per call plus 60 input and 40 output tokens per record,
and answers every call in 20 ms. That makes the token and cost columns a model of the arithmetic,
not a measurement of a real API. In particular the seconds for `records_per_call` are optimistic:
a real model takes longer to write 20 answers than one. Batch mode has no meaningful time here: the fake answers at once, a real batch takes minutes to
hours. A run against a real API has not been done yet.

## Caveats

- The HTTP fan-out is likely limited by the test server (Python's `http.server`), not by agentl.
  A real API with more capacity may scale further; this was not measured.
- The agent numbers isolate agentl's overhead. With a real provider, throughput is set by the
  provider's latency and rate limits, so use `concurrency` to the level your plan allows.
- Each scenario runs once per invocation (micro scenarios take the median of 3). Expect noise of
  around 10% between runs on shared machines.

## Footprint

The core install depends only on `pyyaml` and `pydantic`. Everything else is an extra, imported
only when a step uses it. `tests/test_footprint.py` fails if a new core dependency appears or if
`import agentl` starts loading a heavy module.
