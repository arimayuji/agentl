# Concepts

## Everything is a step

A step receives records (`list[dict]`) from its inputs and returns records. Extract, transform and
load are labels, not different machinery.

| Step kind | YAML | Use for |
|---|---|---|
| Connector | `use: pdf.read` | Reading and writing files, APIs and databases |
| Function | `fn: my_module:clean` | Your own deterministic code |
| Agent | `agent: "..."` + `output:` | Semantic work: classify, extract, normalize, audit |

## Steps form a graph

`input` points to one step or a list of steps. That makes ETL, ELT, fan-out and joins the same thing:

```yaml
steps:
  - { id: a, use: csv.read, with: { path: a.csv } }
  - { id: b, use: json.read, with: { path: b.json } }
  - { id: ab, use: join, input: [a, b], with: { left: a, right: b, on: id } }
```

The runner executes steps in dependency order and refuses cycles.

## Extract, transform and load sections

If you prefer the classic shape, use sections instead of `steps`. Inside `transform` and `load`, a step
without `input` reads from the previous one:

```yaml
extract:
  - { id: raw, use: csv.read, with: { path: in.csv } }
transform:
  - { use: select, with: { fields: [id, total] } }
load:
  - { use: json.write, with: { path: out.json } }
```

## Environment variables

`${VAR}` is replaced from the environment when the file is loaded. `${VAR:-default}` provides a
fallback. A missing variable without a default is an error.

## Cache and resume

Agent steps cache each record's result in `.agentl/agentl.db`, keyed by the instruction, the schema,
the model and the record. Re-running a pipeline only pays for records that changed or failed.
Set `cache: true` on other steps to cache them too, or run with `--no-cache` to ignore the cache.

## Events

Every run emits `run.started`, `step.started`, `step.succeeded`, `step.failed`, `item.rejected`,
`source.empty`, `run.succeeded` and `run.failed`. They feed the run store, the dashboard and
[notifications](notifications.md).
