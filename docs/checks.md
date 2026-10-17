# Data checks

A pipeline that runs at 5 am and writes the report someone opens at 8 should not be trusted
blindly. A `check` step looks at the data before it is delivered: fixed expectations catch the
obvious, an optional agent review catches the odd, and a failure stops the run before yesterday's
good file is overwritten.

```yaml
name: daily-sales
llm:
  default: { provider: anthropic, model: claude-opus-5-5 }

steps:
  - id: sales
    use: postgres.read
    with: { dsn: "${DATABASE_URL}", query: "select * from sales where day = current_date - 1" }

  - id: verify
    check: sales
    expect:
      rows: { min: 1000, max: 50000 }
      not_null: [order_id, total]
      unique: order_id
      accepted: { region: [north, south, east, west] }
      range: { total: { min: 0 } }
      fresh: { field: day, max_age: 2d }
      vs_last_run: { rows: 30%, sum: { total: 40% } }
    review: >
      Daily sales report for the finance team. Flag anything that looks wrong:
      totals out of the usual range, a region with no orders, odd categories.
    on_fail: stop            # or warn

  - id: report
    use: excel.write
    input: verify
    with:
      path: "smb://fileserver/finance/sales_{date}.xlsx"
      latest: "smb://fileserver/finance/sales.xlsx"
      keep: 30

notify:
  channels:
    finance: { type: teams, webhook: "${TEAMS_WEBHOOK}" }
  rules:
    - on: check.failed
      to: [finance]
```

The check passes the records through unchanged, so the writer reads from it (`input: verify`).

## Fixed expectations

Deterministic and free. Each failure explains itself, with the numbers.

| Expectation | Example | Fails when |
| --- | --- | --- |
| `rows` | `{ min: 1000, max: 50000 }` | the row count is outside the range |
| `not_null` | `[order_id, total]` | a column has empty values |
| `unique` | `order_id` or `[a, b]` | a column has duplicates |
| `accepted` | `{ region: [north, south] }` | a column has a value outside the list |
| `range` | `{ total: { min: 0, max: 10000 } }` | a number is outside the range |
| `fresh` | `{ field: day, max_age: 1d }` | the newest date is older than `max_age` (`30m`, `6h`, `1d`, `2w`) |
| `vs_last_run` | `{ rows: 30%, sum: { total: 40% } }` | the row count, or a column's sum, changed more than that since the last run that passed |
| `rejected_rate` | `"< 1%"` or `{ orders: "<= 0.5%" }` | a step rejected a larger share of its records in this run (see [contracts](#schema-contracts)) |

`vs_last_run` compares with the profile of the last run whose check passed, kept in the run store.
On the first run there is nothing to compare with, so it passes.

## Agent review

With `review:`, an agent step looks at the data too. It does not read every row: it gets a profile
(row count; nulls, distinct values, min, max, mean and sum or the most common values for each
column; 15 sample rows), the profile of the last delivered run, and the fixed checks that failed.
That keeps the cost to one small call per run.

The agent answers with a verdict and findings in plain language, for example *"The north region
has no orders today; on the last run it had 312."* When it passes, its findings are kept as
`notes`. Its tokens count in the run's budget and in the step's spend.

## When a check fails

- `on_fail: stop` (default): the step fails with the findings, so nothing after it runs and the
  last good output stays in place.
- `on_fail: warn`: the data goes on and the findings are reported.

Both emit `check.failed` with `findings` (a list of `{check, detail}`), so any notification rule
can route them; a passing check emits `check.passed`. Both show on the dashboard's timeline.

## Layout drift

When a supplier changes a file's format, an old parser fails obscurely or writes wrong data. A
check with `on_drift:` records the layout of its input (columns in order and their types) per
pipeline and step, and compares each run with the last one:

```yaml
- id: sheet_ok
  check: { schema_matches: sheet, on_drift: fail }   # warn | fail | remap
  input: sheet
```

`check: sheet` with `on_drift: fail` on the step is the same, and `on_drift` combines with
`expect` and `review`. The first run records the layout; an empty input is skipped. When the
layout changes, the step emits `schema.drift` with:

- `added` and `removed`: column names.
- `renamed`: `[old, new]` pairs, a guess: a removed and an added column of the same type with a
  similar name or at the same position.
- `type_changed`: `{column: [old type, new type]}`. Types are `bool`, `int`, `float`, `str`,
  `date`, `datetime`, `list`, `dict`; mixed columns join them (`int|str`). A column with no values
  in one of the runs is not a type change.

Then:

- `on_drift: warn`: the data goes on and the new layout becomes the one on record.
- `on_drift: fail`: the step fails, nothing after it runs, and the old layout stays on record,
  so every run fails until the source is fixed.
- `on_drift: remap`: the step fails like `fail`, and the event also carries `previous` and
  `current` layouts for a new reviewed mapping. To accept the new layout once the mapping is
  updated, run once with `warn`.

Route it like any event: `notify: { rules: [{ on: schema.drift, to: [ops] }] }`. Inputs on the
Arrow fast path are read from the Table's schema, without converting it first.

## Schema contracts

A contract checks each record; a check looks at the whole dataset. Put a `schema:` on the step
that brings the data in, so a bad row stops there instead of crashing a later step or reaching
the warehouse:

```yaml
steps:
  - id: orders                       # source
    use: http.get
    with: { url: "https://api.example.com/orders" }
    schema:                          # contract, per record
      id: int
      total: { money: BRL }
      status: { enum: [paid, pending, refunded] }
      email: email?                  # `?`: may be missing or empty
    on_bad: reject                   # reject | coerce | fail
    extra: keep                      # keep | drop | forbid
    rejected_to: { use: json.write, with: { path: rejected/orders.json } }

  - id: verify                       # check, per dataset
    check: orders
    expect:
      rows: { min: 100 }
      rejected_rate: "< 1%"

  - id: load                         # delivery
    use: postgres.write
    input: verify
    with: { dsn: "${DATABASE_URL}", table: orders }
```

The types are the ones of [`cast`](connectors.md#clean-types-cast), written the same way. A field
is required unless its type ends in `?`.

- `on_bad: reject` (default): a record whose values are not already clean values of their type
  (`"12"` for an `int`, a status outside the enum, a missing `id`) leaves the output. It goes to the
  step's rejected records with `_error` (the reason, per field) and `_raw` (the offending values),
  as agent and `cast` rejects do: an `item.rejected` event, the quarantine (`agentl rejected`),
  the dashboard and `agentl stats` count it.
- `on_bad: coerce`: values are converted as `cast` converts them (`"12"` becomes 12,
  `"R$ 1.234,56"` becomes 1234.56, dates become ISO text); what cannot be converted is rejected.
  Numbers follow pt-BR unless the field says otherwise: `{ type: decimal, locale: en-US }`.
- `on_bad: fail`: the first bad record stops the run, with the step, the record's position and the
  field in the error.
- `extra`: fields the schema does not name are kept (default), dropped, or make the record bad
  (`forbid`).
- `rejected_to`: once the step is done, all its rejected records of the run (from the contract or
  from the step itself) go to that writer, so nothing is lost. Nothing is written when there are
  none.

The contract runs after the step. On an agent step, `output:` still shapes and validates what the
model answers; `schema:` then checks the result like any other step's. In a chunked chain
(`chunk_size`) it runs on every chunk, and `rejected_rate` adds up all chunks. A step with a
`schema:` outputs records, not an Arrow table: the fast path stops at that step and the next steps
read records.

**Contract or check?** Use a contract for what each record must be (types, required fields,
allowed values), and to set bad records aside while the good ones go on. Use a check for what the
dataset as a whole must look like (row counts, duplicates, freshness, changes since the last run,
how many records were rejected), and to stop delivery when it does not.
