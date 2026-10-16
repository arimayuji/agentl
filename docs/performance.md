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

agentl 0.10.0 (with #159), Python 3.13, x86_64, 4 CPUs (a shared cloud container), median of 3.
Peak MB is how much the process peak RSS (`VmHWM`) grew during the job; read and written MB are
bytes through system calls (`/proc/self/io`, Linux only), so they include the run state agentl
keeps in SQLite.

**CSV → drop empty notes → keep 3 columns → SQLite**, 1M rows (70 MB CSV)

| Variant | Seconds | CPU s | Peak MB | Read MB | Written MB |
| --- | --- | --- | --- | --- | --- |
| agentl, `chunk_size: 50000` | 4.07 | 3.94 | 64 | 67.3 | 33.3 |
| plain Python, `csv` + `sqlite3` in batches | 2.77 | 2.69 | 15 | 65.7 | 32.9 |
| pandas, `read_csv` + `to_sql` | 4.78 | 4.64 | 574 | 65.8 | 32.9 |

**One LLM call per record**, 500 records, fake provider with 20 ms latency

| Variant | Seconds | CPU s | Peak MB | Read MB | Written MB |
| --- | --- | --- | --- | --- | --- |
| agentl, `concurrency: 8` | 1.38 | 0.15 | 6 | 1.1 | 0.4 |
| agentl, second run (answers from the cache) | 0.03 | 0.02 | 0 | 0.1 | 0.3 |
| plain Python, a loop | 10.09 | 0.03 | 0 | 0.0 | 0.0 |
| plain Python, 8 threads | 1.28 | 0.05 | 1 | 0.0 | 0.0 |

**CSV → pandas function → Parquet**, 500k rows

| Variant | Seconds | CPU s | Peak MB | Read MB | Written MB |
| --- | --- | --- | --- | --- | --- |
| agentl, `@dataframe` with the Arrow path | 0.74 | 1.10 | 268 | 32.1 | 2.9 |
| pandas only | 0.94 | 0.94 | 128 | 20.1 | 2.5 |

What this says:

- On plain record work agentl costs time: about 1.5x the hand-written loop on the CSV job, and a
  little faster than pandas. It streams in bounded memory (64 MB against 574 MB for pandas), with
  run state, retries and the event log. A tight script that streams by hand is faster and smaller.
- On LLM work the time is the provider's. agentl matches 8 hand-written threads and is 7x faster
  than a loop; the gain over hand-written code is the cache (a re-run of unchanged records makes
  no calls), budgets and spend tracking, not raw speed.
- With pandas, the Arrow path reads CSV with pyarrow and was faster than `pd.read_csv` here, but
  peaks at about twice pandas' memory (Arrow table plus DataFrame). Its time swings between runs
  (0.6 to 1.0 s seen), as pyarrow uses several threads on a shared machine.
- Earlier versions of this page showed 0 MB for several jobs. That was the measurement: Linux
  carries `ru_maxrss` over from the parent process, so a child's growth read 0 whenever the
  parent had been bigger. The numbers above use `VmHWM`, which starts fresh in each process.

## HTTP and HTML against the usual libraries

Same rules as above: fresh process per variant, identical output required. The HTTP cases hit a
local server that answers every request after 10 ms, so they measure the client, not the internet.

| Job | agentl | Others |
| --- | --- | --- |
| paginated API, 100 pages → JSON (10,000 rows) | 1.28 s, 8 MB | urllib 1.20 s, requests 1.20 s, httpx 1.25 s |
| one request per record (500), 16 at a time | 1.23 s, 7 MB | requests loop 5.71 s, requests 16 threads 2.17 s, httpx async 1.18 s |
| HTML table → CSV (20,000 rows) | 0.66 s, 19 MB | BeautifulSoup html.parser 1.69 s / 72 MB, BeautifulSoup lxml 1.17 s / 69 MB, pandas `read_html` 0.48 s / 49 MB |

How to read it:

- Pagination is one request after another, so it is bound by latency: every client lands near
  100 × 10 ms plus start-up. agentl is about 6% behind the plain loops.
- With one request per record, agentl's `concurrency` is 4.6x a plain requests loop and ahead of
  a hand-written thread pool, level with httpx async (which needs async code). These numbers swing
  between runs.
- On HTML tables `html.read` is about 2x faster than BeautifulSoup with a third of its memory;
  pandas `read_html` is faster still.
- The test server sets `TCP_NODELAY`. Without it, Nagle plus delayed ACKs added ~40 ms per request
  to keep-alive clients (requests, httpx) and not to urllib, which would have measured the server.

## Regression gate

Every pull request that touches `agentl/` or `benchmarks/` runs the comparison at full size twice
over on the same runner: with its own agentl and with the base branch's, checked out next to it.

```bash
git worktree add ../agentl-base origin/main
python -m benchmarks --compare --against ../agentl-base
```

For each gated job (CSV to SQLite, LLM per record, Parquet, one request per record, HTML table)
the run alternates the two agentls run by run, so drift on a shared machine hits both. It fails
when this agentl takes more than 20% longer than the base (25% for the HTTP and HTML jobs, 75%
for the sub-second Parquet job, whose time swings), or peaks more than 25% plus 10 MB above it.

An earlier version compared against a committed ratio to plain Python. That ratio did not hold
across machines on the CPU-bound CSV job (1.3x locally, 1.8x to 2.2x on CI runners with the same
code), so it failed on main. Comparing agentl with agentl on one machine removes the hardware from
the question. `--check` and `--write-baseline` still exist for comparing runs by hand.

`--quick` is not used for the gate: on 10% of the data agentl's start-up cost swamps the job.

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
