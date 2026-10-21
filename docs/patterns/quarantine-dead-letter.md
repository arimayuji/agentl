# Quarantine and dead letter

**Category:** Reliability

**Problem.** One malformed row should not stop the load of thousands of good ones, and it should not disappear silently either.

## Diagram

```text
csv.read --> cast (on_bad: reject) --> check (accepted: reject) --> sqlite.write
                 |                          |
                 +---------> quarantine <---+   (agentl rejected, outcome partial)
```

## When to use

- Bad rows are expected and someone can fix them later.
- The good rows are useful on their own.

## When not to

- A bad row means the whole batch is wrong (a broken export): fail the run instead (`on_fail: stop`).

## Trade-offs

- The run ends `succeeded` with outcome `partial`, which alerts by default. Watch `rejected_rate` so the quarantine does not grow unnoticed.
- Rejected rows keep `_error` and `_raw`, so the fix is visible.

## Recipe

Start from it with no LLM call:

```bash
agentl new --template quarantine-dead-letter -o pipeline.yaml
```

[Folder with the sample data](https://github.com/arimayuji/agentl/tree/main/examples/patterns/quarantine-dead-letter/)

```yaml
--8<-- "examples/patterns/quarantine-dead-letter/pipeline.yaml"
```

It runs in CI on every change, offline, with the sample data in its folder (`tests/test_recipes.py`).

## Related

- [Data contract at the boundary](data-contract.md)
- [Rules before the model](rules-before-model.md)
- [Severity per rule](../checks.md#severity-per-rule)
- [Run outcomes](../concepts.md#run-outcomes)
