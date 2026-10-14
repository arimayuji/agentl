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
    use: csv.write
    input: verify
    with: { path: "smb://fileserver/finance/sales.csv" }

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
