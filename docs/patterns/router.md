# Router

**Category:** LLM steps

**Problem.** Mixed inputs (invoices, contracts, noise) need different extraction, and one prompt for all of them does each badly.

## Diagram

```text
json.read --> route (rules, then llm)
                 |--invoice--> invoices (extract total)   --+
                 |--contract--> contracts (extract parties) --+--> concat --> json.write
                 +--other--> drop
```

## When to use

- Inputs fall into a few known kinds, each with its own fields.
- Some kinds can be decided by a rule (a field value) before the model.

## When not to

- The split is a plain field value for every record: use `filter` branches (fan-out) and no LLM.

## Trade-offs

- One classification call per record not decided by rules, plus each branch's calls.
- An answer outside the labels is retried, then quarantined.

## Recipe

Start from it with no LLM call:

```bash
agentl new --template router -o pipeline.yaml
```

Needs: An LLM (the page uses OpenRouter; change the `llm` block to your provider).

[Folder with the sample data](https://github.com/arimayuji/agentl/tree/main/examples/patterns/router/)

```yaml
--8<-- "examples/patterns/router/pipeline.yaml"
```

It runs in CI on every change, offline, with the sample data in its folder (`tests/test_recipes.py`).

## Related

- [Fan-out and fan-in](fan-out-fan-in.md)
- [Rules before the model](rules-before-model.md)
- [Confidence cascade](confidence-cascade.md)
- [Route each record to one branch](../agents.md#route-each-record-to-one-branch)
