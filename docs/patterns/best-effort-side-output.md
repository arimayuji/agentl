# Best-effort side outputs

**Category:** Reliability

**Problem.** A secondary destination (a legacy table, an export for another team) fails now and then, and should not take the main load down with it.

## Diagram

```text
csv.read --> sqlite.write (main, required)
        \--> csv.write (legacy, required: false, after: main)
```

## When to use

- A destination is nice to have, or is being migrated away from.
- The main destination must not wait for, or fail because of, the side one.

## When not to

- Both destinations must stay consistent: make both required, or write one from the other.

## Trade-offs

- A failed side output is recorded (`step.failed` with `required: false`, outcome `partial`) and nothing is rolled back.
- No step may read a best-effort step; use `after:` to order.

## Recipe

Start from it with no LLM call:

```bash
agentl new --template best-effort-side-output -o pipeline.yaml
```

[Folder with the sample data](https://github.com/arimayuji/agentl/tree/main/examples/patterns/best-effort-side-output/)

```yaml
--8<-- "examples/patterns/best-effort-side-output/pipeline.yaml"
```

It runs in CI on every change, offline, with the sample data in its folder (`tests/test_recipes.py`).

## Related

- [Fan-out and fan-in](fan-out-fan-in.md)
- [Retries with backoff, then a fallback source](fallback-source.md)
- [Required and best-effort steps](../concepts.md#required-and-best-effort-steps)
