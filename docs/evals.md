# Evals

The [benchmarks](performance.md) measure speed, memory and I/O with a fake model. Evals answer
the questions that matter for LLM pipelines: is the answer right, what does it cost per record,
and how much does the cache save when you run again.

```bash
python -m evals --provider anthropic --model claude-haiku-5-5 --price-in IN --price-out OUT
python -m evals --provider openai --model MODEL --price-in IN --price-out OUT --runs 5 --json out.json
python -m evals                       # offline, fake provider: tests the harness, not a result
```

Prices are USD per million tokens, taken from your provider's price page. They are not guessed:
without them, cost shows as n/a. Evals are not run in CI (they need a key and cost money).

## Datasets

Both live in `evals/data/`, are synthetic and are small on purpose (40 records each), so a run
costs cents.

| Dataset | Task | Fields scored |
| --- | --- | --- |
| `tickets.jsonl` | classify support tickets, hand-labeled | `category` (5 classes), `urgent` |
| `invoices.jsonl` | extract from invoice texts in four styles, BRL amounts and three date formats; built by `python -m evals.make_invoices` (seeded) | `tax_id`, `total`, `due_date` |

A field counts as right when it matches the label after normalization: case and spaces for text,
digits only for `tax_id`, two decimals for numbers. "All fields" is the share of records with
every field right.

## Variants

Same model, same instruction and same output schema in all of them:

| Variant | What it is |
| --- | --- |
| plain Python loop | the baseline: the provider client called directly in 8 threads, no pipeline |
| agentl | an agent step with `concurrency: 8` |
| agentl, `records_per_call: 10` | ten records per model call |

Each runs `--runs` times (default 3); accuracy is the mean with its spread, since model answers
vary between runs. Then the cache: the agentl pipeline runs once with the cache on and again with
0%, 10% and 50% of the records changed, reporting calls avoided, cost saved and time.

## What to expect, and what it does not show

- agentl and the plain loop should score the same: agentl does not change what the model sees.
  If they differ beyond the run-to-run spread, that is a bug to report.
- `records_per_call` cuts calls and tokens per record; whether accuracy holds depends on the model
  and task. That trade-off is what the eval is for.
- The cache numbers depend on how much of your data changes between runs, not on the model.
- 40 synthetic records do not stand for your data. Copy the harness, put 50 to 200 of your own
  labeled records in `evals/data/`, and run it before choosing a model or `records_per_call`.

## Results

No run against a real provider has been published yet. When one is, it goes here with the model
id, date, prices and the JSON report.
