# Retries with backoff, then a fallback source

**Category:** Reliability

**Problem.** A source has transient failures and, sometimes, longer outages. The run should survive both without a person.

## Diagram

```text
http.get --(retry 3x, backoff)--> ok ----------------------------+
     |                                                            +--> json.write
     +-- still failing --> fallback: csv.read (backup) -- _source -+
```

## When to use

- There is a second source with the same shape: a mirror, a cache, yesterday's export.
- Transient errors (timeouts, 5xx, 429) are common.

## When not to

- The backup is stale in a way that matters: fail and alert instead.
- The step is a writer: a failed write may have written part of the data (needs `fallback_writer`).

## Trade-offs

- A run that used the fallback ends with outcome `partial`. `fallback_tag` marks each record with `_source`.
- Retries add latency; `max_backoff` bounds it.

## Recipe

Start from it with no LLM call:

```bash
agentl new --template fallback-source -o pipeline.yaml
```

[Folder with the sample data](https://github.com/arimayuji/agentl/tree/main/examples/patterns/fallback-source/)

```yaml
--8<-- "examples/patterns/fallback-source/pipeline.yaml"
```

It runs in CI on every change, offline, with the sample data in its folder (`tests/test_recipes.py`).

## Related

- [Best-effort side outputs](best-effort-side-output.md)
- [Quarantine and dead letter](quarantine-dead-letter.md)
- [Retries and fallbacks](../concepts.md#retries-and-fallbacks)
