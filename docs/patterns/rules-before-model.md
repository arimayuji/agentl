# Rules before the model

**Category:** LLM steps

**Problem.** Most values of a messy field are clean. Sending every record to an LLM pays for the easy ones too.

## Diagram

```text
csv.read --> agent step
               rules (compute, cast) --ok--> _path: rules
                         \--miss--> llm --> _path: agent
           --> csv.write
```

## When to use

- A regex, a cast or a small function handles most records.
- The rest need judgement.

## When not to

- Rules that match but give a wrong value never reach the model: test them on a sample first.

## Trade-offs

- Cost and latency drop with the share the rules handle; `spend[step]["paths"]` counts both.
- Rules and agent must give the same fields; `output:` checks both paths.

## Recipe

Start from it with no LLM call:

```bash
agentl new --template rules-before-model -o pipeline.yaml
```

Needs: An LLM (the page uses OpenRouter; change the `llm` block to your provider).

[Folder with the sample data](https://github.com/arimayuji/agentl/tree/main/examples/patterns/rules-before-model/)

```yaml
--8<-- "examples/patterns/rules-before-model/pipeline.yaml"
```

It runs in CI on every change, offline, with the sample data in its folder (`tests/test_recipes.py`).

## Related

- [Parsers first, the agent last](parsers-first.md)
- [Confidence cascade](confidence-cascade.md)
- [Router](router.md)
- [Rules first, the agent for the rest](../agents.md#rules-first-the-agent-for-the-rest)
