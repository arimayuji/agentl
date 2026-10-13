# CLI and dashboard

```text
agentl run PIPELINE [--no-cache] [--show STEP]
agentl validate PIPELINE
agentl diagram PIPELINE
agentl runs
agentl connectors
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

## Diagram

`agentl diagram` prints the pipeline as Mermaid. Connectors are boxes, functions are rounded and
agents are hexagons:

```mermaid
flowchart LR
  notas["notas"] --> cruzar("cruzar")
  clientes["clientes"] --> cruzar
  cruzar --> classificar{{"classificar"}}
  classificar --> salvar["salvar"]
```

## Dashboard

`agentl dashboard` serves a local page with recent runs, the event log and the pipeline diagram colored
by step status. It binds to `127.0.0.1` by default. Do not expose it publicly: it has no authentication.
