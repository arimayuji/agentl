# Changeset write

**Category:** Data movement

**Problem.** Rewriting a whole table fires triggers, bloats history and hides what changed. Deleting rows that left the source is error-prone.

## Diagram

```text
csv.read --> sqlite.write (mode: changeset, key: [month, product])
                 |
                 +-- inserted / updated / unchanged, deletes only in the months the source sends
```

## When to use

- A source sends full partitions (a month, a region) and rows can disappear from them.
- Downstream needs the counts or the diff (`emit: changes`).

## When not to

- Append-only events: a plain append or upsert is simpler.
- The table does not fit in memory with `sql.write` (SQLite and Postgres diff in the database).

## Trade-offs

- One transaction per write, set-based diff. Equal rows are not touched, so a re-run writes nothing.
- `checksums: true` skips unchanged partitions but trusts that nothing else writes to the table.

## Recipe

Start from it with no LLM call:

```bash
agentl new --template changeset-write -o pipeline.yaml
```

[Folder with the sample data](https://github.com/arimayuji/agentl/tree/main/examples/patterns/changeset-write/)

```yaml
--8<-- "examples/patterns/changeset-write/pipeline.yaml"
```

It runs in CI on every change, offline, with the sample data in its folder (`tests/test_recipes.py`).

## Related

- [Change history with SCD type 2](scd2-history.md)
- [Idempotent re-run](idempotent-rerun.md)
- [Changeset writes](../connectors.md#changeset-writes)
