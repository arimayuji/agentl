# CLI and dashboard

```text
agentl run PIPELINE [--no-cache] [--show STEP]
agentl validate PIPELINE
agentl diagram PIPELINE
agentl runs
agentl connectors
agentl new "DESCRIPTION" [-o FILE] [--provider P] [--model M]
agentl doctor [RUN_ID] [--suggest] [--pipeline FILE]
agentl mcp
agentl dashboard [--host 127.0.0.1] [--port 7070]
```

Every command accepts `--db` to choose the run store: a SQLite path or a `postgresql://` URL
(default: `$AGENTL_DB`, then `.agentl/agentl.db`).

## Run store

The run store keeps run history, events and the per-record agent cache.

- **SQLite** (default): one local file, nothing to set up.
- **Postgres**: one store shared by every worker, so the dashboard shows all runs and a record
  processed by one worker is not paid for again by another. Needs `agentl-etl[postgres]`.

```bash
export AGENTL_DB=postgresql://etl:${PGPASSWORD}@db.internal:5432/etl
agentl run pipelines/sales.yaml      # any machine, any worker
agentl dashboard                     # sees every run
```

In Python: `agentl.run("pipeline.yaml", db="postgresql://...")` or `agentl.store.open_store(url)`.
Postgres tables are prefixed (`agentl_runs`, `agentl_events`, `agentl_cache`), so the store can live
in an existing database. They are created on first use; the user needs `CREATE` on the schema.

`validate` parses the YAML, checks the graph and that every connector exists. It does not connect to
databases or URLs. It does resolve `${VAR}`, so every variable without a default must be set; in CI,
export placeholders (`DATABASE_URL=postgresql://ci@localhost/ci agentl validate ...`).

## New pipeline from a description

```bash
agentl new "read invoices.csv, classify each line by cost center, write to Postgres" -o invoices.yaml
```

`new` sends the description and the list of installed connectors to an LLM, validates the YAML it
answers and sends any validation error back for another try (3 attempts). The file is only written
once it validates, and never over an existing file. The diagram goes to stderr. The model is
`--provider` / `--model`, else `$AGENTL_PROVIDER` / `$AGENTL_MODEL`, else Anthropic with the
default model (needs `ANTHROPIC_API_KEY`). Review the result before running it: it is a first
draft, not a tested pipeline.

## Doctor

```bash
agentl doctor                 # the latest failed run
agentl doctor 20261014-091203-ab12cd --suggest --pipeline invoices.yaml
```

`doctor` reads the run store: the step that failed, its error, check findings, rejected records
and the steps that finished. No LLM is called unless you pass `--suggest`, which adds a suggested
fix (with `--pipeline`, the model also sees the YAML).

## Diagram

`agentl diagram` prints the pipeline as Mermaid. Connectors are boxes, functions are rounded and
agents are hexagons:

```mermaid
flowchart LR
  invoices["invoices"] --> matched("matched")
  customers["customers"] --> matched
  matched --> classify{{"classify"}}
  classify --> save["save"]
```

## Dashboard

<video class="feature-clip" autoplay muted loop playsinline preload="none" poster="../assets/clips/dashboard.jpg" aria-label="The dashboard during a live run: steps light up, KPIs and spend update, events stream in">
  <source src="../assets/clips/dashboard.webm" type="video/webm">
  <source src="../assets/clips/dashboard.mp4" type="video/mp4">
</video>

<small>Screen recording of `agentl dashboard` during a run (offline demo pipeline with a fake LLM).</small>

`agentl dashboard` serves a local page, refreshed every second: recent runs, the pipeline graph with
live status, records, duration and cost per step, KPIs, spend per step and the event timeline. It is
self-contained (no CDN) and follows the newest run until you pick one. It binds to `127.0.0.1` by default. Do not expose it publicly: it has no authentication.
