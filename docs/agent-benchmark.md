# Agent benchmark

Does a coding agent do ETL work better with agentl than with plain Python? This benchmark gives
the same ten tasks to an agent twice, once told to use agentl and once told to use Python without
it, and checks the output of every run against the expected records.

!!! note "No results published yet"
    The harness and the tasks are ready and tested; the runs with a real agent cost money and have
    not been made yet. Results will be added here with every run listed, the spread between runs and
    the exact agent and model versions, including the tasks where agentl loses.

## Tasks

| Task | What the agent must do |
|---|---|
| 01-filter-paid-orders | CSV, keep paid orders above 100, write JSON with typed fields |
| 02-join-orders-customers | join a CSV and a JSON on a key, keep orders with no customer |
| 03-dedupe-spreadsheet | read an Excel sheet, rename columns, drop duplicates |
| 04-paginated-api | read every page of a JSON API until `next_page` is null |
| 05-pdf-invoice-totals | read PDF invoices, parse Brazilian money (`R$ 1.250,00`) |
| 06-html-table | turn an HTML table into CSV |
| 07-database-totals | SQLite group by with counts and sums |
| 08-flatten-json | nested JSON to one row per order, with counts and sums of a list |
| 09-combine-daily-files | combine one CSV per day into one file |
| 10-order-numbers-from-emails | pull order numbers out of free text, null when absent |

Each task folder in
[`benchmarks/agent_tasks`](https://github.com/arimayuji/agentl/tree/main/benchmarks/agent_tasks)
has the request (`task.md`), the inputs and `expected.json`. A reference solution in plain Python
solves all ten through the harness in CI, so every task is known to be solvable and the scorer is
known to accept a right answer.

## How a run is scored

- The inputs are copied to an empty folder; tasks that call an API get a local HTTP server.
- The agent gets the request on stdin and works in that folder.
- The output file is read (JSON or CSV) and its records are matched by key. A task passes when every
  expected record is there with every expected field equal (numbers as numbers, empty and null
  alike) and there are no extra rows. Extra columns are fine.
- With Claude Code's `--output-format json`, cost, tokens and turns are recorded too.

Arms:

- `agentl`: the [agentl skill](ai-agents.md#claude-code-skill) is installed in the folder and the
  request starts with "Use agentl for this...".
- `plain`: the request starts with "Use Python (the standard library, pandas or any installed
  package) but not agentl". agentl stays installed in the environment; the instruction is what
  differs, which is how the comparison happens in real projects.

## Run it

```bash
pip install -e ".[dev]"        # openpyxl and pypdf for the reference checks
python -m benchmarks.agent_bench run --arm both --runs 3
python -m benchmarks.agent_bench report benchmarks/agent-results.jsonl
```

The default agent is Claude Code headless (`claude -p --output-format json --allowedTools
Bash,Read,Write,Edit,Glob,Grep`); pass `--agent "<command>"` for any other agent that reads the
request on stdin. Each run records the model(s) that did the work. To pin one, add it to the
command, e.g. `--agent "claude -p --model sonnet --output-format json --allowedTools ..."`. With
Claude Code the reported cost is the API price of the run; on a subscription it counts against the
plan's usage instead of being billed. Thirty runs per arm (ten tasks, three runs) is the smallest set worth publishing:
agents vary between runs, so one run of each says little.
