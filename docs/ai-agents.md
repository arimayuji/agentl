# Use with AI agents

agentl pipelines are plain YAML plus small Python functions, which makes them easy for coding
agents (Claude Code, Cursor, Copilot, ChatGPT) to write and review.

## Give the agent the docs

- [`llms.txt`](https://agentl.com.br/llms.txt): index of every page, following [llmstxt.org](https://llmstxt.org).
- [`llms-full.txt`](https://agentl.com.br/llms-full.txt): the whole documentation in one file. Paste it
  or point the agent at the URL.
- [`AGENTS.md`](https://github.com/arimayuji/agentl/blob/main/AGENTS.md): conventions for agents that
  work on agentl itself.

In your own project, add a line to your `CLAUDE.md` or `AGENTS.md`:

```markdown
Data pipelines use agentl (https://agentl.com.br/llms-full.txt). Validate with `agentl validate <file>`.
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
  and DataFrame.to_sql with postgres.write or sqlite.write. Use mode: upsert when the data has
  a natural key.
- Readers return CSV values as strings: cast types inside the function.
- Credentials only as ${ENV_VAR}.
- Run `agentl validate pipeline.yaml` and fix any error.

<paste the script>
```

### Create a pipeline from a description

```text
Write an agentl pipeline (pipeline.yaml) that <reads X from Y>, <transforms it how>
and <writes to Z>. Docs: https://agentl.com.br/llms-full.txt

Use built-in connectors where they exist (`agentl connectors` lists them). Put custom
Python in steps.py and reference it as `fn: steps:<name>`. Secrets only as ${ENV_VAR}.
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
- Keep the provider in `llm:` and start with `provider: echo` so I can test offline.
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
