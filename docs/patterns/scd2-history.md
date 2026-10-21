# Change history with SCD type 2

**Category:** Data movement

**Problem.** Overwriting a dimension loses its past: last quarter's report cannot say which segment a customer was in then.

## Diagram

```text
csv.read --> sqlite.write (mode: scd2, key: customer_id, track: [city, segment])
                 |
                 +-- changed: close current version, insert new one; missing: close
```

## When to use

- Reports must join facts with the dimension as it was at the time.
- Only a few tracked columns matter for history.

## When not to

- Nobody asks about the past: an upsert is cheaper and simpler.
- Columns change on every load (timestamps): leave them out of `track`.

## Trade-offs

- The table grows with every change. Queries must filter `is_current` or a date range.
- Re-running the same data changes nothing.

## Recipe

Start from it with no LLM call:

```bash
agentl new --template scd2-history -o pipeline.yaml
```

[Folder with the sample data](https://github.com/arimayuji/agentl/tree/main/examples/patterns/scd2-history/)

```yaml
--8<-- "examples/patterns/scd2-history/pipeline.yaml"
```

It runs in CI on every change, offline, with the sample data in its folder (`tests/test_recipes.py`).

## Related

- [Changeset write](changeset-write.md)
- [History of a dimension (SCD type 2)](../connectors.md#history-of-a-dimension-scd-type-2)
