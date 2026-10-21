# Confidence cascade

**Category:** LLM steps

**Problem.** A strong model on every record is expensive; a cheap model alone is wrong on the hard ones.

## Diagram

```text
csv.read --> agent: cheap model --confidence >= 0.8--> answer
                        \--low or invalid--> strong model --> answer
          --> csv.write
```

## When to use

- Most records are easy and a cheap model is enough for them.
- The model can report a useful confidence, or answers are checkable by schema.

## When not to

- Self-reported confidence is not calibrated for the task: measure with a golden set first.

## Trade-offs

- Cost is the cheap model on all records plus the strong one on the escalated share.
- Latency grows for escalated records.

## Recipe

Start from it with no LLM call:

```bash
agentl new --template confidence-cascade -o pipeline.yaml
```

Needs: An LLM (the page uses OpenRouter; change the `llm` block to your provider).

[Folder with the sample data](https://github.com/arimayuji/agentl/tree/main/examples/patterns/confidence-cascade/)

```yaml
--8<-- "examples/patterns/confidence-cascade/pipeline.yaml"
```

It runs in CI on every change, offline, with the sample data in its folder (`tests/test_recipes.py`).

## Related

- [Rules before the model](rules-before-model.md)
- [Router](router.md)
- [Cheap model first](../agents.md#cheap-model-first)
- [Evals](../evals.md)
