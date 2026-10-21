# Data contract at the boundary

**Category:** Reliability

**Problem.** Bad data that gets past the source step crashes a later step or, worse, reaches the warehouse.

## Diagram

```text
csv.read + schema (per record, on_bad, extra) --> check (rows, unique, rejected_rate) --> sqlite.write
```

## When to use

- Data comes from a team or a system you do not control.
- Types matter downstream (numbers, dates, enums).

## When not to

- Exploration of a new source: start without a contract, then write it from what you saw.

## Trade-offs

- The schema rejects or coerces per record; the check decides per dataset. Together they stop at the door.
- `extra: drop` hides new columns; use `keep` or `forbid` if you want to notice them.

## Recipe

Start from it with no LLM call:

```bash
agentl new --template data-contract -o pipeline.yaml
```

[Folder with the sample data](https://github.com/arimayuji/agentl/tree/main/examples/patterns/data-contract/)

```yaml
--8<-- "examples/patterns/data-contract/pipeline.yaml"
```

It runs in CI on every change, offline, with the sample data in its folder (`tests/test_recipes.py`).

## Related

- [Quarantine and dead letter](quarantine-dead-letter.md)
- [Parsers first, the agent last](parsers-first.md)
- [Schema contracts](../checks.md#schema-contracts)
- [Data checks](../checks.md)
