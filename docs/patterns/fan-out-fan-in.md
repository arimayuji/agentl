# Fan-out and fan-in

**Category:** Data movement

**Problem.** Different slices of the data need different treatment, and doing it in one long step mixes the rules.

## Diagram

```text
            +--> filter (online) --> compute (net) --+
csv.read --+                                         +--> concat --> csv.write
            +--> filter (store)  --> compute (net) --+
```

## When to use

- Branches are independent and can run in parallel.
- Each branch has its own rules, then the results meet again.

## When not to

- The split depends on a judgement call: use a router.
- The branches only differ by a parameter: one `compute` with an expression may be enough.

## Trade-offs

- Independent steps run in parallel. A failure in one branch fails the run unless the branch is best effort.
- `concat` appends in input order; records keep the fields of their branch.

## Recipe

Start from it with no LLM call:

```bash
agentl new --template fan-out-fan-in -o pipeline.yaml
```

[Folder with the sample data](https://github.com/arimayuji/agentl/tree/main/examples/patterns/fan-out-fan-in/)

```yaml
--8<-- "examples/patterns/fan-out-fan-in/pipeline.yaml"
```

It runs in CI on every change, offline, with the sample data in its folder (`tests/test_recipes.py`).

## Related

- [Router](router.md)
- [Best-effort side outputs](best-effort-side-output.md)
- [Steps form a graph](../concepts.md#steps-form-a-graph)
- [Transforms](../connectors.md)
