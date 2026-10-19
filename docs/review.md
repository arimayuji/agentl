# Human review queue

An agent step answers every record, including the ones it is unsure about. `review:` sends those
answers to a person instead of passing them on, and reuses the person's answer the next time the
same input comes in.

```yaml
- id: classify
  input: load
  agent: Classify the expense.
  output: { category: str, confidence: float }
  review: { below: 0.8 }
```

## When an answer is queued

| Option   | Queues an answer when                                                        |
|----------|------------------------------------------------------------------------------|
| `below`  | its `confidence` is below this value, or missing (needs a `confidence: float` output field) |
| `when`   | the [expression](agents.md) is true on the answered record, e.g. `"category == 'other'"` |
| `always` | `true`: every answer the llm gives                                            |
| `hold`   | `true` (default): the record is held back; `false`: it goes on, marked        |

Options combine: an answer is queued when any rule matches. A `when` that fails to evaluate counts
as a match, so a broken rule sends records to a person rather than letting them through.

Only answers the llm gave are checked: records that `rules:` handled, records the step rejected
and answers a person already gave are not queued again.

## Held or marked

With `hold: true` (the default, and the safe choice) a queued record does **not** reach the next
steps. The run succeeds with the other records, and the step's spend in the run summary and the
`step.succeeded` event carries `review: {queued, held, flagged, reused, dropped}`. A held record
is not lost: it waits in the queue, and the next run after a person reviews it sends the reviewed
answer downstream.

With `hold: false` the record goes on with `_review: pending`, and is queued anyway. Use it when a
late correction is acceptable, for example when a writer upserts by key and the next run corrects
the row.

The same input is queued once. A held record whose input comes again before anyone reviewed it
stays held, and nobody is asked twice.

## Review from the CLI

```bash
agentl review list                                 # pending answers, every pipeline
agentl review list --pipeline expenses --step classify --status all
agentl review show 12                              # input, model answer, reason, as JSON
agentl review accept 12 13 14                      # the model was right
agentl review fix 15 category=travel               # values are read as JSON, else as text
agentl review fix 16 --value '{"category": "travel"}'
agentl review reject 17                            # drop the record
agentl review export --golden golden/classify.jsonl --pipeline expenses --step classify
```

Each queued item keeps the run id, the pipeline, the step, the input record (secret values
masked as in the [rejected records](cli.md#rejected-records)), the model's answer and the reason.
`fix` accepts only fields the step outputs. The queue lives in the run store, in the `review`
table (`agentl_review` on Postgres).

## Reuse of the answers

An accepted or fixed answer is stored with the record's agent cache key, in the queue and in the
agent cache. On later runs a record with the same input (and the same prompt, output and llm
settings, as for the cache) takes the person's answer with no llm call, and the step's spend counts
it under `review.reused`. A rejected record is dropped on later runs without an llm call
(`review.dropped`). Changing the prompt, the output fields or the llm changes the key, so the step
asks the llm again.

People's answers are used even with `agentl run --no-cache`, since they are decisions rather than
cached model output.

## Golden sets from corrections

`agentl review export --golden FILE` appends every accepted and fixed answer to a
[golden set](agents.md#golden-sets-test-a-prompt-or-model-change): the input record plus the
answer, without the model's `confidence`. Lines the file already has are skipped, so the export
can run after every review session. Then `agentl eval` tells you whether a new prompt or model gets
those records right.

## Events and stats

- `review.pending` (step, `queued`, `held`, `flagged`) when a run held or marked records, so a
  [notification](notifications.md) can tell someone the queue has work.
- `review.reused` (step, `reused`, `dropped`) when people's answers replaced llm calls.
- `agentl stats` shows the queue per pipeline (pending and reviewed) and, per agent step, the
  answers queued, held and reused. The dashboard shows the pending count under each pipeline.
