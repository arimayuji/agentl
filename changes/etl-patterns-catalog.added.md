An ETL patterns catalog in the docs (`docs/patterns/`): 14 named patterns (incremental cursor,
idempotent re-run, changeset write, SCD2, fan-out and fan-in, quarantine, raw zone and replay,
retries with fallback, best-effort side outputs, data contract, parsers first, rules before the
model, confidence cascade, router), each with a diagram, when to use it and when not to, and a
pipeline in `examples/patterns` that runs offline in CI. `agentl new --list` shows them in a
`patterns` group, and `agentl new --template <pattern>` starts from one (#307).
