# Parsers first, the agent last

**Category:** LLM steps

**Problem.** A semi-structured file changes layout between versions. Parsers break on the new layout, and an LLM on every file is slow and costly.

## Diagram

```text
first_valid:
  csv_export --(fails validate)--> json_api --(passes)--> rows (_path: json_api)
                                       \--(fails)--> agent (last resort)
```

## When to use

- Most inputs match a known layout, and new layouts appear now and then.
- A wrong but plausible output can be told apart by `validate` (rows, required fields, types).

## When not to

- The decision is per record, not per document: use rules before the model.
- No parser exists yet: start with the agent and add parsers as layouts settle.

## Trade-offs

- The LLM only runs when every parser fails; `alert_agent_share` alerts when that happens too often.
- `validate` is the safety net: a weak one lets plausible garbage through.

## Recipe

Start from it with no LLM call:

```bash
agentl new --template parsers-first -o pipeline.yaml
```

[Folder with the sample data](https://github.com/arimayuji/agentl/tree/main/examples/patterns/parsers-first/)

```yaml
--8<-- "examples/patterns/parsers-first/pipeline.yaml"
```

It runs in CI on every change, offline, with the sample data in its folder (`tests/test_recipes.py`).

## Related

- [Rules before the model](rules-before-model.md)
- [Data contract at the boundary](data-contract.md)
- [Parsers first, the agent last](../agents.md#parsers-first-the-agent-last)
