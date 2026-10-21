# Idempotent re-run

**Category:** Data movement

**Problem.** Schedulers retry, people re-run by hand, and the same file arrives twice. Each repeat must not duplicate rows.

## Diagram

```text
csv.read --> skip_seen (key: payment_id) --> sqlite.write (upsert on payment_id)
                  |
                  +-- keys marked only when the whole run succeeds
```

## When to use

- Inputs can arrive more than once: files in a folder, webhooks, retried jobs.
- Rows have a stable business key.

## When not to

- There is no stable key: hash the record or the file (`key: sha256`) or use `mode: replace`.

## Trade-offs

- `skip_seen` saves work; the upsert is the real guarantee, so keep it even with `skip_seen`.
- A failed run handles the same records again next time, by design.

## Recipe

Start from it with no LLM call:

```bash
agentl new --template idempotent-rerun -o pipeline.yaml
```

[Folder with the sample data](https://github.com/arimayuji/agentl/tree/main/examples/patterns/idempotent-rerun/)

```yaml
--8<-- "examples/patterns/idempotent-rerun/pipeline.yaml"
```

It runs in CI on every change, offline, with the sample data in its folder (`tests/test_recipes.py`).

## Related

- [Incremental load with a cursor](incremental-cursor.md)
- [Changeset write](changeset-write.md)
- [Seen files](../cli.md#seen-files)
- [Upsert](../connectors.md#postgresql)
