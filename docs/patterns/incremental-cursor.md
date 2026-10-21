# Incremental load with a cursor

**Category:** Data movement

**Problem.** Reading the whole source on every run is slow and expensive, and most rows have not changed.

## Diagram

```text
http.get (since={cursor}) --> sqlite.write (upsert on id)
      |
      +-- cursor: max(updated_at), saved only when the run succeeds
```

## When to use

- The source can filter by a monotonic field: `updated_at`, an id, a sequence.
- Runs are frequent and each one sees a small share of the data.

## When not to

- The source revises old rows without moving the field: add a lookback window instead.
- The source cannot filter at all: compare with a changeset write.

## Trade-offs

- Cheap and fast per run. A failed run does not move the cursor, so the next one repeats the window: the writer must be idempotent (upsert).
- Deletes in the source are invisible to a cursor.

## Recipe

Start from it with no LLM call:

```bash
agentl new --template incremental-cursor -o pipeline.yaml
```

[Folder with the sample data](https://github.com/arimayuji/agentl/tree/main/examples/patterns/incremental-cursor/)

```yaml
--8<-- "examples/patterns/incremental-cursor/pipeline.yaml"
```

It runs in CI on every change, offline, with the sample data in its folder (`tests/test_recipes.py`).

## Related

- [Idempotent re-run](idempotent-rerun.md)
- [Changeset write](changeset-write.md)
- [Raw zone and replay](raw-zone-replay.md)
- [Incremental loading: cursor](../concepts.md#incremental-loading-cursor)
