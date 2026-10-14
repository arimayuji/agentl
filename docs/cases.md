# Before and after

Four pipelines that teams write all the time, each in two versions: the script people usually
write (pandas, a loop calling the model, sqlite3) and the agentl pipeline. Both run on the same
data with the same model and must write **the same result**; the harness checks it.

```bash
python -m cases            # all four, offline, about 10 seconds
python -m cases --only tickets --json cases.json
```

The model is a stand-in with a fixed 20 ms per call and deterministic answers, so results can be
compared exactly and every call counted. With a real model, the cost follows the call counts.

## Results

| Case | Records | Lines before | Lines with agentl | Model calls | Calls to recover from a failed write | Calls to re-run unchanged data |
| --- | --- | --- | --- | --- | --- | --- |
| Support ticket triage | 40 | 8 | 17 | 40 | 80 → 40 | 40 → 0 |
| Invoice field extraction | 40 | 15 | 21 | 40 | 80 → 40 | 40 → 0 |
| Web pages to summaries | 6 | 19 | 19 | 6 | 12 → 6 | 6 → 0 |
| Daily sales report to a shared folder | 4 | 18 | 30 (YAML 22 + pandas 8) | 0 | 0 → 0 | 0 → 0 |

"A failed write" means the output was unavailable on the first try (a locked file, a share that is
down, a full disk), then fixed, and the job run again. The hand-written script pays every model
call again; agentl only re-runs what did not finish, because each answer is cached per record.
"Re-run unchanged data" is the same job run again on the same input: the script calls the model
for every record, agentl for none.

## What it does not show

- **Lines of code are not lower with agentl.** The YAML is about as long as a minimal script, or
  longer. The scripts here are the short version: no retries, no rate limiting, no budget, no log
  of what ran. Adding those to the script is where it grows; with agentl they are already there.
- **Speed:** the scripts call the model in a plain loop, agentl with `concurrency: 8`, so agentl
  finishes first (2x to 5x in the LLM cases here). A script with a thread pool would close that gap; the
  [benchmarks](performance.md#with-agentl-and-without-it) measure it.
- **The sales report has no model calls,** so the cache does not apply. What agentl adds there is
  the check before publishing and the versioned output, which the script also does by hand in
  fewer lines. Pick agentl for it if the rest of your pipelines are agentl; on its own, the script
  is fine.

## What each version handles

| | Script | agentl |
| --- | --- | --- |
| Same result | yes | yes |
| Re-run only pays for what changed | no | yes, per-record cache |
| Retries on 429 / 5xx from the model | no | yes |
| Calls in parallel | no (a loop) | `concurrency` |
| Stop at a spend limit | no | `budget` |
| Validated output shape from the model | by hand | `output` schema |
| Run history, dashboard, `agentl stats` | no | yes |
| Data check before delivery | by hand (sales report) | `check` steps |

## The code

### Support ticket triage

A CSV of tickets, one model call per ticket for category and urgency, an Excel file for the team.

=== "Before (Python)"

    ```python
    --8<-- "cases/tickets/before.py"
    ```

=== "With agentl (YAML)"

    ```yaml
    --8<-- "cases/tickets/pipeline.yaml"
    ```


### Invoice field extraction

Invoice texts, the model extracts tax id, total and due date, loaded into a database.

=== "Before (Python)"

    ```python
    --8<-- "cases/invoices/before.py"
    ```

=== "With agentl (YAML)"

    ```yaml
    --8<-- "cases/invoices/pipeline.yaml"
    ```


### Web pages to summaries

HTML pages stripped to their main text, one summary per page, saved as JSON.

=== "Before (Python)"

    ```python
    --8<-- "cases/web_summaries/before.py"
    ```

=== "With agentl (YAML)"

    ```yaml
    --8<-- "cases/web_summaries/pipeline.yaml"
    ```


### Daily sales report to a shared folder

A messy daily export: drop blanks and repeated orders, check it, total by region, publish a dated Excel file with a latest copy and the last 7 versions. No LLM.

=== "Before (Python)"

    ```python
    --8<-- "cases/sales_report/before.py"
    ```

=== "With agentl (YAML)"

    ```yaml
    --8<-- "cases/sales_report/pipeline.yaml"
    ```

=== "With agentl (pandas step)"

    ```python
    --8<-- "cases/sales_report/steps.py"
    ```

