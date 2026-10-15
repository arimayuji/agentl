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

agentl 0.6.0, Python 3.13, x86_64, 4 CPUs (a shared cloud container). Your numbers will differ:
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
| `arrow csv-pandas-parquet` | 6.47 s with dicts, 0.63 s with Arrow (10.3x), identical Parquet | 500k-row CSV through `csv.read` → `@dataframe` → `parquet.write` |
| `arrow parquet-postgres` | 4.69 s INSERT, 0.21 s COPY from Arrow (22x), identical rows | 200k rows, `parquet.read` → `postgres.write`; needs `AGENTL_TEST_PG_DSN` |
| `parallel steps` | 1.08 s one at a time, 0.51 s in parallel | Two independent 0.5 s sources and a step on one of them, `max_parallel_steps` 1 vs 4 |
| `agent cascade` | 0.69 USD with one strong model, 0.13 with `llm: [cheap, strong]` (5.2x less) | 500 records, 350 distinct (both deduplicated to 350); the cheap model fails one record in ten, which goes to the strong one |
| `agent call shapes` | 0.99 USD default, 0.45 with prompt caching, 0.42 with `records_per_call: 20`, 0.50 in batch mode | 500 records, fake provider with a 400-token fixed prefix (see below) |

## With agentl and without it

`python -m benchmarks --compare` runs the same jobs written three ways: with agentl, in plain
Python as a careful developer would write it, and with pandas where it fits. Each variant runs in
a fresh process and the run fails unless every variant wrote the same data. The plain variants do
not import agentl. Imports and setup happen before the measurement starts.

```bash
python -m benchmarks --compare            # full run, median of 3
python -m benchmarks --compare --quick    # 10% of the data, one run
```

agentl 0.6.1, Python 3.13, x86_64, 4 CPUs (a shared cloud container). Peak MB is the growth of
the process peak RSS during the job; read and written MB are bytes through system calls
(`/proc/self/io`, Linux only), so they include the run state agentl keeps in SQLite.

**CSV → drop empty notes → keep 3 columns → SQLite**, 1M rows (70 MB CSV)

| Variant | Seconds | CPU s | Peak MB | Read MB | Written MB |
| --- | --- | --- | --- | --- | --- |
| agentl, `chunk_size: 50000` | 5.33 | 5.13 | 60 | 67.2 | 33.3 |
| plain Python, `csv` + `sqlite3` in batches | 3.01 | 2.90 | 0 | 65.7 | 32.9 |
| pandas, `read_csv` + `to_sql` | 4.00 | 3.88 | 156 | 65.8 | 32.9 |

**One LLM call per record**, 500 records, fake provider with 20 ms latency

| Variant | Seconds | CPU s | Peak MB | Read MB | Written MB |
| --- | --- | --- | --- | --- | --- |
| agentl, `concurrency: 8` | 1.37 | 0.14 | 0 | 0.9 | 0.4 |
| agentl, second run (answers from the cache) | 0.03 | 0.02 | 0 | 0.1 | 0.3 |
| plain Python, a loop | 10.10 | 0.03 | 0 | 0.0 | 0.0 |
| plain Python, 8 threads | 1.28 | 0.05 | 0 | 0.0 | 0.0 |

**CSV → pandas function → Parquet**, 500k rows

| Variant | Seconds | CPU s | Peak MB | Read MB | Written MB |
| --- | --- | --- | --- | --- | --- |
| agentl, `@dataframe` with the Arrow path | 0.63 | 0.97 | 0 | 32.0 | 2.9 |
| pandas only | 0.97 | 0.96 | 0 | 20.1 | 2.5 |

What this says:

- On plain record work agentl costs time: about 1.8x the hand-written loop and 1.3x pandas on the
  CSV job, for streaming in bounded memory (60 MB against 156 MB for pandas), run state, retries
  and the event log. A tight script that streams by hand is faster and uses less memory.
- On LLM work the time is the provider's. agentl matches 8 hand-written threads and is 7x faster
  than a loop; the gain over hand-written code is the cache (a re-run of unchanged records makes
  no calls), budgets and spend tracking, not raw speed.
- With pandas, the Arrow path reads CSV with pyarrow and was faster than `pd.read_csv` here; it
  reads more bytes because of the run state.
- Peak MB of 0 means the job did not grow the process past what imports and setup had used.

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
