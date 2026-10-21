# Raw zone and replay

**Category:** Reliability

**Problem.** Sources rarely keep history. When a transform had a bug, last month's data is gone and cannot be reprocessed.

## Diagram

```text
http.get (persist: raw) --> compute --> json.write
      |
      +-- objects/<sha256>.json, immutable

agentl replay --from-raw RUN_ID: read the original back, skip the source, run the rest
```

## When to use

- The source is volatile: an API with no history, a page that changes.
- Transforms evolve and old runs must be reprocessed with the new code.

## When not to

- The payload holds secrets or personal data that must not be kept: agentl skips outputs with a secret, but decide on purpose.
- The source keeps its own history and is cheap to read again.

## Trade-offs

- Disk grows with distinct payloads (identical ones are stored once); `raw.keep_days` prunes.
- A replay runs the sinks again: keep them idempotent.

## Recipe

Start from it with no LLM call:

```bash
agentl new --template raw-zone-replay -o pipeline.yaml
```

[Folder with the sample data](https://github.com/arimayuji/agentl/tree/main/examples/patterns/raw-zone-replay/)

```yaml
--8<-- "examples/patterns/raw-zone-replay/pipeline.yaml"
```

It runs in CI on every change, offline, with the sample data in its folder (`tests/test_recipes.py`).

## Related

- [Incremental load with a cursor](incremental-cursor.md)
- [Idempotent re-run](idempotent-rerun.md)
- [Keep originals and replay them](../orchestration.md#keep-originals-and-replay-them)
