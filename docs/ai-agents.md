# Use with AI agents

agentl pipelines are plain YAML plus small Python functions, which makes them easy for coding
agents (Claude Code, Cursor, Copilot, ChatGPT) to write and review.

## Give the agent the docs

- [`llms.txt`](https://agentl.com.br/llms.txt): index of every page, following [llmstxt.org](https://llmstxt.org).
- [`llms-full.txt`](https://agentl.com.br/llms-full.txt): the whole documentation in one file, code
  of the examples included. Paste it or point the agent at the URL.
- [Recipes](recipes.md): 20 short pipelines, tested in CI, for agents to start from.
- [`AGENTS.md`](https://github.com/arimayuji/agentl/blob/main/AGENTS.md): conventions for agents that
  work on agentl itself.

In your own project, add this to your `CLAUDE.md` or `AGENTS.md`:

```markdown
## Data pipelines
- Extract, transform and load data with agentl (YAML pipelines, docs: https://agentl.com.br/llms.txt).
- List connectors with `agentl connectors`; never invent one.
- Before running: `agentl lint <file> --format json` (fix every finding), `agentl preview <file> <step>`
  to check a transform, `agentl plan <file>` to see LLM calls and cost.
- Secrets only as ${ENV_VAR}; never print or ask for their values.
```

## Claude Code skill

agentl ships a [skill](https://docs.claude.com/en/docs/claude-code/skills) that teaches Claude
Code the pipeline format, the write, lint, preview, plan and run loop, and the rules (budgets,
checks before delivery, no secrets in YAML), so it uses agentl well without reading the docs first:

```bash
agentl skill install            # ~/.claude/skills/agentl, for every project
agentl skill install --project  # ./.claude/skills/agentl, committed with this project
agentl skill show               # print it
```

## MCP server

`agentl mcp` serves agentl over MCP (stdio), so Claude Code, Claude Desktop, Cursor and other MCP
clients can validate, run and debug your pipelines directly.

One command sets it up in your client, without editing JSON:

```bash
agentl mcp install --client claude-desktop     # or claude-code, cursor, vscode, windsurf
agentl mcp install --client cursor --project   # this folder only (claude-code, cursor, vscode)
agentl mcp status                              # which clients have it, and the command they run
agentl mcp uninstall --client cursor
```

It finds the client's config file for your OS, adds or updates the `agentl` entry and keeps every
other entry. Before a change it writes a backup next to the file (`<file>.bak`); running it twice
changes nothing; a file with comments or trailing commas is left alone with a message. The entry
runs the Python you ran the command with, so the client gets the same agentl version and extras;
`--uvx` uses `uvx agentl-etl` instead. `--root`, `--allow-code` and `--db` are passed to the
server; a `--db` URL with a password is refused (set `AGENTL_DB` in the client's environment). For
Claude Code it uses `claude mcp add-json` when the `claude` CLI is installed. Restart the client
afterwards; the command says how.

By hand:

```bash
claude mcp add agentl -- agentl mcp            # Claude Code
claude mcp add agentl -- uvx agentl-etl mcp    # without installing it first (needs uv)
```

From 0.9.0 on, agentl is published to the
[official MCP registry](https://registry.modelcontextprotocol.io) as `io.github.arimayuji/agentl`,
so clients that browse the registry can find and install it.

```json
{"mcpServers": {"agentl": {"command": "agentl", "args": ["mcp"]}}}
```

The second form goes in Claude Desktop's or Cursor's MCP settings. Add `"--db", "<store>"` to the
args to point at a run store other than `.agentl/agentl.db` in the working directory.

| Tool | What it does |
| --- | --- |
| `profile_file` | A local data file under `--root`: rows, columns, the type each reads as, null rates, a sample and a suggested `cast:` step |
| `validate` | Check a pipeline (a file path or the YAML text): every error with line, code and fix |
| `lint` | Validate plus best-practice warnings, as JSON findings |
| `preview_step` | The first records into and out of a step (readers and transforms only, nothing written) |
| `plan` | Records per step, cache hits, LLM calls, token and cost ranges, what would be written |
| `list_templates` | Tested pipeline templates, name and what each does |
| `new_from_template` | Write a template to a file under the server's root (no LLM call) |
| `new_pipeline` | Paid option: a validated pipeline YAML from a description (calls the LLM set by `AGENTL_PROVIDER`) |
| `diagram` | The pipeline as Mermaid |
| `run_pipeline` | Run a pipeline file; returns status, run id, records per step and cost |
| `show_output` | The first records a step produced in a run (at most 5, values cut at 200 characters) |
| `list_artifacts` | What recent runs wrote: per writer step the file or table and its rows, plus records per step |
| `list_runs`, `get_run` | Recent runs; one run with its events |
| `list_failures` | Recent failed runs with the failing step and error |
| `doctor` | Why a run failed, as text |
| `repair` | Paid option: a fix for a failed run as a checked unified diff, never applied |
| `report` | A bug report draft for a failed run: issue-form markdown, prefilled issue URL, secrets masked |
| `pipeline_stats` | Success rate, durations, cost and failures by reason per pipeline; `labels` filters runs by label |
| `list_connectors` | Installed connectors, one line each |
| `describe_connector` | One connector's `with:` parameters, types, defaults and notes |

### Resources and prompts

Clients that list resources can read these on demand instead of fetching the website:

| Resource | What it is |
| --- | --- |
| `agentl://docs/skill` | The agent guide: how to build, check and run pipelines |
| `agentl://docs/connectors` | Installed connectors, one line each |
| `agentl://docs/schema` | JSON Schema of a pipeline file |
| `agentl://templates/<name>` | A tested template, e.g. `agentl://templates/incremental-api-load` |
| `agentl://connectors/<name>` | One connector's `with:` parameters, e.g. `agentl://connectors/csv.read` |
| `agentl://pipelines/<path>` | A pipeline YAML file under `--root` (listed when it has a top-level `steps:`) |
| `agentl://runs`, `agentl://runs/<run_id>` | The latest runs; one run's summary (status, failing step, error) |

Prompts show as shortcuts in clients that list them: `build_pipeline` (asks for the sources and
the destination, then works from the templates), `explain_failed_run` and `estimate_cost` (calls
`plan`, never runs the pipeline).

### Over HTTP

Web and cloud agents that cannot start a local command can use streamable HTTP (MCP 2025-06-18):

```bash
export AGENTL_MCP_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
agentl mcp --http :7072 --root ./pipelines
```

The endpoint is `http://127.0.0.1:7072/mcp`. Each POST carries one JSON-RPC message and gets one
JSON answer; the server opens no event stream (GET answers 405). When `AGENTL_MCP_TOKEN` (or the
variable named by `--token-env`) is set, every request needs `Authorization: Bearer <token>`, and
gets 401 without it. The same `--root` sandbox and `--allow-code` rule apply as over stdio.

To expose it safely:

- it binds to `127.0.0.1` by default; it refuses to listen on another address without a token;
- requests with a browser `Origin` that is not localhost are refused (DNS rebinding);
- the server speaks plain HTTP: to reach it from another machine put it behind a TLS reverse
  proxy or a tunnel (Cloudflare Tunnel, Tailscale, SSH `-L`), not straight on a public port;
- keep `--allow-code` off, point `--root` at the pipelines folder only, and rotate the token if
  it leaks. Anyone with the token can run the pipelines under `--root` with your credentials.

`run_pipeline` and `new_from_template` are the tools marked as not read-only, so clients that ask
before side effects ask before running a pipeline or writing a file. It runs with your environment and permissions, so it is limited:

- it only runs pipeline files under the folder the server started in (`--root` to change it), never
  YAML text sent by the client (`plan` and `preview_step` follow the same rule, since they read
  the sources, and `profile_file` only reads data files under it);
- `show_output` reads a sample the runner keeps in the run store: the first 5 records of each
  step, values cut at 200 characters. Secret values that end up in the data are replaced by `***`
  first: cloud secret references, and environment variables the pipeline used whose names look
  like secrets (`KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `DSN`, `AUTH`, `CREDENTIAL`...). Personal
  data in the records is kept as it is, so set `AGENTL_SAMPLES=0` to keep none;
- it refuses pipelines that run code on your machine (`fn:` steps, Python tools, local MCP server
  commands) unless you start the server with `agentl mcp --allow-code`;
- `validate` and `diagram` see `${VAR}` and secret references as placeholders, so their answers
  never carry a value from your environment back to the model.

## Report a bug or fix it with your agent

When agentl itself fails (an internal error, a missing connector, a confusing message), ask your
agent to draft the issue: the `report` MCP tool, or `agentl report --pipeline p.yaml` in a terminal,
builds it locally and sends nothing. `agentl report --submit` files it with the user's `gh`
after asking, and shows existing issues with the same fingerprint instead of a duplicate; an agent
asks the user before running it. To go on to a fix pull request, point the agent at
[CONTRIBUTING_AI.md](https://github.com/arimayuji/agentl/blob/main/.github/CONTRIBUTING_AI.md):
setup, where connectors and steps live, how to add a test, the checks and the rules.

```text
agentl failed with an error that looks like a bug. Use the agentl `report` tool for the latest
failed run with pipeline.yaml, shrink the repro to the smallest pipeline with made-up data, show
me the issue before filing it, then follow CONTRIBUTING_AI.md to propose a fix.
```

## Prompts

Copy, fill the parts in `<angle brackets>`, and paste. Each prompt asks the agent to run
`agentl validate`, so mistakes surface before the first run.

### Migrate a pandas script

```text
Migrate this pandas script to an agentl pipeline. Docs: https://agentl.com.br/llms-full.txt

Rules:
- Keep my transform logic in pandas, inside a function decorated with
  agentl.pandas.dataframe, in steps.py next to the pipeline.
- Replace pd.read_* with agentl reader connectors (csv.read, json.read, excel.read, file.read)
  and DataFrame.to_sql with postgres.write (append, replace, upsert) or sqlite.write (append,
  replace). Use mode: upsert when rows have
  a natural key, and mode: replace for aggregates that are recomputed in full.
- Readers return CSV values as strings: cast types inside the function.
- Credentials only as ${ENV_VAR}.
- Run `agentl lint pipeline.yaml --format json` (export placeholder values for ${VARS}) and fix
  every finding: each one has the line, the code and the fix.

<paste the script>
```

### Create a pipeline from a description

```text
Write an agentl pipeline (pipeline.yaml) that <reads X from Y>, <transforms it how>
and <writes to Z>. Docs: https://agentl.com.br/llms-full.txt

Use built-in connectors where they exist (`agentl connectors` lists them). Put custom
Python in steps.py and reference it as `fn: steps:<name>`. Secrets only as ${ENV_VAR}.
Relative paths resolve from the directory where agentl runs.
Add a notify rule for run.failed. Run `agentl validate pipeline.yaml` and
`agentl diagram pipeline.yaml`, then show me both outputs.
```

### Add an agent step that validates a column

```text
In <pipeline.yaml>, add an agent step after <step id> that checks the column <name>
and flags records that look wrong. Docs: https://agentl.com.br/agents/

- Declare an `output:` schema with the original key, the checked value, `valid: bool`
  and `reason: str?`.
- Write the instruction as a short, specific task, with one example of a bad value.
- Keep the provider in `llm:` and start with `provider: echo` so I can test offline. Echo cannot
  invent values, so make the new fields optional (`bool?`, `str?`) for the offline run.
- Run `agentl validate` and `agentl run --show <new step id>`.
```

### Review a pipeline

```text
Review <pipeline.yaml> against the agentl docs (https://agentl.com.br/llms-full.txt).
Look for: secrets written in plain text, writes that duplicate rows on re-run (append without
a key), agent steps without an output schema, steps that could be a built-in connector instead
of custom code, and missing failure notifications. List findings, most severe first.
```

## Tips

- Ask for `agentl validate` and `agentl diagram` in every prompt: they are cheap and catch most
  mistakes.
- Keep agent steps for the fuzzy parts (classify, extract, normalize free text). Deterministic
  work belongs in connectors and functions, where it is faster and free.
- Review generated YAML like any code before it touches production data.
