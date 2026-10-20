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
| `unique` | `order_id`, `[a, b]` or `[[date, currency]]` | a column, or a composite key, has duplicates (below) |
| `accepted` | `{ region: [north, south] }` | a column has a value outside the list |
| `range` | `{ total: { min: 0, max: 10000 } }` | a number is outside the range |
| `fresh` | `{ field: day, max_age: 1d }` | the newest date is older than `max_age` (`30m`, `6h`, `1d`, `2w`) |
| `vs_last_run` | `{ rows: 30%, sum: { total: 40% } }` | the row count, or a column's sum, changed more than that since the last run that passed |
| `vs_runs` | `{ last: 5, rows: 30%, sum: { total: 40% } }` | the row count, or a column's sum, is further than that from the median of the last `last` runs that passed |
| `rejected_rate` | `"< 1%"` or `{ orders: "<= 0.5%" }` | a step rejected a larger share of its records in this run (see [contracts](#schema-contracts)) |
| `pii` | `true` or `{ allow: [customer_email] }` | a field looks like unprotected personal data (below); `allow` lists fields that may carry it |

`pii` samples up to 200 records and flags a field when at least half of its values match one
detector: CPF and CNPJ (check digits), US SSN (`123-45-6789`, format only), e-mail, phone (BR or US,
written with separators or `+`), payment card (Luhn), IBAN (mod 97), IP address, and person names in
fields named like `name`, `full_name` or `last_name`. Values already passed through
[`protect`](connectors.md#protect-sensitive-fields-protect) no longer match. The failure names the
field, the kind, a masked example (`*******4321`, never the full value) and the suggested `protect`
action. From Python, `agentl.pii.scan(records)` returns the findings and
`agentl.pii.suggest_protect(findings)` the `fields` for a `protect` step. Detection is heuristic:
expect misses (free-text fields, unformatted phones) and some false positives.

`unique` takes a column or a list. A flat list checks each column on its own: `[date, currency]`
fails as soon as two rows share a date, even with different currencies. A nested list is one
composite key, unique as a whole: `[[date, currency]]` allows many currencies per date and many
dates per currency, but not the same pair twice. The two forms mix:
`unique: [id, [date, currency]]` checks `id` alone and the pair. Rows with an empty value in a key
column are left to `not_null`. The failure gives the number of duplicates and of duplicated keys,
with up to three example keys; values of fields a `protect` step in the pipeline marks show as
`<protected>`. With `severity: reject`, the first row of each key is kept and the later ones are
rejected.

`vs_last_run` compares with the profile of the last run whose check passed, kept in the run store.
On the first run there is nothing to compare with, so it passes.

`vs_runs` compares with the median of the last `last` passing runs (default 5, at most 50), so
one unusual run does not move the baseline. With fewer passing runs than `last` it uses the ones
there are; with none it passes.

### Severity per rule

By default a failing rule fails the check (and `on_fail` decides what that does). `severity` sets
it per rule:

```yaml
- id: verify
  check: orders
  expect:
    rows: { min: 1000 }
    not_null: [order_id]
    accepted: { region: [north, south] }
    vs_runs: { last: 5, rows: 30% }
  severity:
    vs_runs: warn        # emit `check.warned`, let the data through
    accepted: reject     # drop the failing rows into the quarantine
    # rows and not_null: fail (the default)
```

| Severity | What it does |
| --- | --- |
| `fail` | the check fails: stops before the writer, or warns with `on_fail: warn` |
| `warn` | a `check.warned` event with the findings; the data goes through |
| `reject` | the failing rows are dropped and quarantined like rejected records (`item.rejected`); only for `not_null`, `unique`, `accepted` and `range` |

`review` takes `fail` or `warn` too. The `check.passed` and `check.failed` events carry `rules`,
the result of each rule in this run: `passed`, `failed`, `warn` or `reject`.

## Agent review

With `review:`, an agent step looks at the data too. It does not read every row: it gets a profile
(row count; nulls, distinct values, min, max, mean and sum or the most common values for each
column; 15 sample rows), the profile of the last delivered run, and the fixed checks that failed.
That keeps the cost to one small call per run.

The agent answers with a verdict and findings in plain language, for example *"The north region
has no orders today; on the last run it had 312."* When it passes, its findings are kept as
`notes`. Its tokens count in the run's budget and in the step's spend.

## Row rules and the daily digest

`rules:` checks each row. Every rule has an `on_fail`: `drop` leaves the row out (it goes to the
step's rejected records), `flag` keeps it and reports it (use it when two values disagree and it
is unclear which one is wrong), `fail` stops the step. An error inside a rule (a value that is not
a number, say) never stops the pipeline: the row passes and the error is logged.

`rules:` means something different by step kind. On a `check` step (this page) it lists per-row
checks (`rule: not_null | between | accepted | matches | abs_diff_max`) that drop, flag or fail
rows. On an `agent` step it lists [hybrid rules](agents.md#rules-first-the-agent-for-the-rest)
(`compute`, `cast`, `fn`) that try to produce the `output:` fields before the llm is asked. A
hybrid rule on a check step, or `rules:` on any other kind of step, is a validation error.

```yaml
- id: verify
  check: transform
  identity: [order_id]          # what makes a finding "the same" from one day to the next
  rules:
    - { rule: between, column: amount, min: 0, max: 100000, on_fail: drop }
    - { rule: abs_diff_max, column: amount, other: billed, max: 500, on_fail: flag }
    - { rule: not_null, column: customer, notify: none }  # flagged, left out of the digest
    - { rule: accepted, column: status, values: [paid, refunded] }
    - { rule: matches, column: email, pattern: "@" }
```

Findings are grouped by identity in a daily (UTC) bucket; runs on the same day add to it. Each run
emits `check.digest` with what is `new`, `resolved` and `ongoing` compared with the previous
day's bucket, the counts, and a `summary`. On a clean day it still goes out with
`all_clear: true` and "all clear: no findings today", so a missing digest means the service did
not run. Route it to any channel with a normal rule:

```yaml
notify:
  channels: { slack: { webhook: "${SLACK_WEBHOOK}" } }
  rules:
    - { on: check.digest, to: [slack] }
```

That message goes out with each run of the check. Each rule also takes `notify`: `digest`
(default) puts its findings in the digest, `none` acts silently (the row is still dropped, flagged
or failed as `on_fail` says, but the finding is left out of the digest).

### Scheduled digest

`notify.digest` sends one digest per period at a set time instead, with a "sent today" guard:

```yaml
notify:
  channels:
    slack: { webhook: "${SLACK_WEBHOOK}" }
    ops: { type: webhook, url: "${OPS_HOOK}" }
  digest:
    to: [slack, ops]                    # any notify channel
    every: 1d                           # whole days: 1d, 7d
    at: "08:00"                         # in tz
    tz: America/Sao_Paulo               # default UTC
    sections: [new, resolved, ongoing]  # which lists to show (default: all three)
    heartbeat: true                     # "all clear" on a clean day (default true)
    idempotent: true                    # guard after the send (default) or before it
```

It reads the daily buckets of every check step with `rules:` and sends, per step, the counts and
the finding ids of each section (up to 50 per section). A step with no run today is reported as
"no run today (last on ...)", which is never a clean day. With `heartbeat: false` a clean day sends
nothing.

The guard is kept per channel in the run store. With `idempotent: true` it is written only after
a successful send, so a channel that failed is tried again on the next check and a channel that
worked is not sent to twice (at least once). With `idempotent: false` it is written before the
send, so a crash in the middle never sends twice, and a failed send is lost (at most once).

Two ways to send it:

- `agentl serve` checks the digest of each pipeline it serves once a minute.
- `agentl digest pipeline.yaml` from cron (any time after `at`; it sends only when due and exits 1
  when a channel failed). `--force` sends now, `--show` prints the digest and sends nothing.

The buckets are UTC days, so with a `tz` far from UTC, `at` should come after the day's check runs.

## When a check fails

- `on_fail: stop` (default): the step fails with the findings, so nothing after it runs and the
  last good output stays in place. It also gates the run's outputs: every final step (one no
  other step depends on, usually a writer) waits for the stop checks of the pipeline, even when
  the check is not on its input path, so a failing check stops a sibling writer too. A
  best-effort check (`required: false`) does not gate.
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
- `on_drift: remap`: the data goes on to the `map_schema` steps that read the check (at least
  one is required), and the event also carries `previous`, `current` and `remap` (those steps).
  For this run they map the new layout as if they had `on_drift: infer`: a mapping already stored
  for that layout is reused with no model call, a pinned `mapping:` is applied as is, and
  otherwise one is inferred. With `review: required` the run stops until
  `agentl mappings approve`. The new layout goes on record only once it has been mapped, so a run
  that stops for review reports the drift again next time.

```yaml
- id: sheet_ok
  check: { schema_matches: sheet, on_drift: remap }
  input: sheet
- id: mapped
  use: map_schema
  input: sheet_ok
  with: { target: { report_date: date, region: str, value: float }, review: required }
```

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
