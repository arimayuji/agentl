# Agent benchmark

Does a coding agent do ETL work better with agentl than with plain Python? This benchmark gives
the same thirteen tasks to an agent in each arm (agentl as the agent chooses, an agentl pipeline,
agentl's Python API, plain Python) and checks the output of every run against the expected records.
Ten are one-off jobs; three are jobs that run again the next day, where what the second run pays
for is measured too.

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
| 11-classify-reruns | classify 50 tickets with an LLM; the next day 10 more arrive (ideal: 10 LLM calls) |
| 12-resume-after-outage | LLM sentiment for 40 reviews; the provider goes down after 25 calls, the next day it is back (ideal: 15 calls) |
| 13-incremental-api | load events from an API with `?since=` into SQLite; an hour later 3 new and 2 changed (ideal: 5 records) |

Tasks 11 to 13 run in two phases in the same folder, like the next day (`task2.md`, and
`input2/` copied over the inputs). Their LLM is a local fake, an OpenAI-compatible API that knows
the answers and counts calls, so they need no key and cost nothing in LLM calls. What the second
phase paid for (LLM calls or API records) is reported next to its ideal.

Each task folder in
[`benchmarks/agent_tasks`](https://github.com/arimayuji/agentl/tree/main/benchmarks/agent_tasks)
has the request (`task.md`), the inputs and `expected.json`. Reference solutions in plain Python and
with agentl's Python API solve all thirteen through the harness in CI, so every task is known to be
solvable and the scorer is known to accept a right answer. A third, `reference_pipeline.py`, solves
11 to 13 as pipelines and reaches each ideal; the plain reference pays for everything again (60,
40 and 33 against 10, 15 and 5).

## How a run is scored

- The inputs are copied to an empty folder; tasks that call an API get a local HTTP server.
- The agent gets the request on stdin and works in that folder.
- The output file is read (JSON, CSV or a SQLite table) and its records are matched by key. A task passes when every
  expected record is there with every expected field equal (numbers as numbers, empty and null
  alike) and there are no extra rows. Extra columns are fine.
- With Claude Code's `--output-format json`, cost, turns and tokens are recorded too. The report
  shows tokens without cache reads: every turn re-reads the conversation from the prompt cache,
  which makes the total grow with turns, while cost already weighs cache reads at their price.

Arms:

- `agentl`: the [agentl skill](ai-agents.md#claude-code-skill) is installed in the folder and the
  request starts with "Use agentl for this"; the agent picks a script or a pipeline, as the skill
  says.
- `agentl-pipeline`: the same, but the request asks for a pipeline (`pipeline.yaml`, `agentl run`).
- `agentl-python`: the request asks for a short script with agentl's Python API.
- `plain`: the request starts with "Use Python (the standard library, pandas or any installed
  package) but not agentl". agentl stays installed in the environment; the instruction is what
  differs, which is how the comparison happens in real projects.

## Run it

```bash
pip install -e ".[dev]"        # openpyxl and pypdf for the reference checks
python -m benchmarks.agent_bench run --arm all --runs 3
python -m benchmarks.agent_bench report benchmarks/agent-results.jsonl
```

The default agent is Claude Code headless (`claude -p --output-format json --allowedTools
Bash,Read,Write,Edit,Glob,Grep`); pass `--agent "<command>"` for any other agent that reads the
request on stdin. Each run records the model(s) that did the work. To pin one, add it to the
command, e.g. `--agent "claude -p --model sonnet --output-format json --allowedTools ..."`. With
Claude Code the reported cost is the API price of the run; on a subscription it counts against the
plan's usage instead of being billed. Thirty-nine runs per arm (thirteen tasks, three runs) is the smallest set worth publishing:
agents vary between runs, so one run of each says little.
