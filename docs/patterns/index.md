# ETL patterns

Named, diagrammed and runnable patterns for pipelines, including the ones with LLM steps.
Each page says when to use the pattern, when not to, and what it costs, and ends with a pipeline
that runs in CI on every change, offline, with its sample data.

Every pattern is also a template, with no LLM call:

```bash
agentl new --list                                  # recipes, then patterns
agentl new --template changeset-write -o pipeline.yaml
```

## Data movement

<div class="pattern-grid" markdown>

<div class="pattern-card" markdown>

[Incremental load with a cursor](incremental-cursor.md)

Reading the whole source on every run is slow and expensive, and most rows have not changed.

</div>

<div class="pattern-card" markdown>

[Idempotent re-run](idempotent-rerun.md)

Schedulers retry, people re-run by hand, and the same file arrives twice. Each repeat must not duplicate rows.

</div>

<div class="pattern-card" markdown>

[Changeset write](changeset-write.md)

Rewriting a whole table fires triggers, bloats history and hides what changed. Deleting rows that left the source is error-prone.

</div>

<div class="pattern-card" markdown>

[Change history with SCD type 2](scd2-history.md)

Overwriting a dimension loses its past: last quarter's report cannot say which segment a customer was in then.

</div>

<div class="pattern-card" markdown>

[Fan-out and fan-in](fan-out-fan-in.md)

Different slices of the data need different treatment, and doing it in one long step mixes the rules.

</div>

</div>

## Reliability

<div class="pattern-grid" markdown>

<div class="pattern-card" markdown>

[Quarantine and dead letter](quarantine-dead-letter.md)

One malformed row should not stop the load of thousands of good ones, and it should not disappear silently either.

</div>

<div class="pattern-card" markdown>

[Raw zone and replay](raw-zone-replay.md)

Sources rarely keep history. When a transform had a bug, last month's data is gone and cannot be reprocessed.

</div>

<div class="pattern-card" markdown>

[Retries with backoff, then a fallback source](fallback-source.md)

A source has transient failures and, sometimes, longer outages. The run should survive both without a person.

</div>

<div class="pattern-card" markdown>

[Best-effort side outputs](best-effort-side-output.md)

A secondary destination (a legacy table, an export for another team) fails now and then, and should not take the main load down with it.

</div>

<div class="pattern-card" markdown>

[Data contract at the boundary](data-contract.md)

Bad data that gets past the source step crashes a later step or, worse, reaches the warehouse.

</div>

</div>

## LLM steps

<div class="pattern-grid" markdown>

<div class="pattern-card" markdown>

[Parsers first, the agent last](parsers-first.md)

A semi-structured file changes layout between versions. Parsers break on the new layout, and an LLM on every file is slow and costly.

</div>

<div class="pattern-card" markdown>

[Rules before the model](rules-before-model.md)

Most values of a messy field are clean. Sending every record to an LLM pays for the easy ones too.

</div>

<div class="pattern-card" markdown>

[Confidence cascade](confidence-cascade.md)

A strong model on every record is expensive; a cheap model alone is wrong on the hard ones.

</div>

<div class="pattern-card" markdown>

[Router](router.md)

Mixed inputs (invoices, contracts, noise) need different extraction, and one prompt for all of them does each badly.

</div>

</div>
