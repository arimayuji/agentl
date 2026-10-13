# Migrate a pandas pipeline

You already have scripts that read a file with pandas, clean it and write to a database. This guide
moves one of them to agentl **without rewriting your pandas code**. You keep the transforms and gain
retries, idempotent loads, run history, notifications and a diagram.

The full example lives in [`examples/sales`](https://github.com/arimayuji/agentl/tree/main/examples/sales).

## 1. The starting point

```python
import os

import pandas as pd
from sqlalchemy import create_engine

df = pd.read_csv("https://example.com/exports/sales.csv", sep=";")
df = df.dropna(subset=["order_id"]).drop_duplicates("order_id")
df["total"] = df["qty"].astype(int) * df["unit_price"].astype(float)
df["country"] = df["country"].str.strip().str.upper()

engine = create_engine(os.environ["DATABASE_URL"])
df.to_sql("sales", engine, if_exists="append", index=False)  # duplicates on re-run
```

It works, but: a network hiccup kills the run, running it twice duplicates rows, nobody is told when
it fails, and there is no record of what ran.

## 2. Split it into extract, transform, load

| pandas | agentl |
| --- | --- |
| `pd.read_csv(url, sep=";")` | `csv.read` with `url:` (delimiter sniffed, retries on 429/5xx) |
| the cleaning lines | your function, wrapped with `@dataframe` |
| `df.to_sql(..., if_exists="append")` | `postgres.write` with `mode: upsert` and `key:` |

## 3. Keep the transform as pandas

`steps.py`, next to the pipeline:

```python
from agentl.pandas import dataframe


@dataframe
def clean(df):
    df = df.replace("", None)  # CSV readers give empty strings, pandas expects NaN
    df = df.dropna(subset=["order_id"]).drop_duplicates("order_id")
    df["order_id"] = df["order_id"].astype(int)
    df["total"] = df["qty"].astype(int) * df["unit_price"].astype(float)
    df["country"] = df["country"].str.strip().str.upper()
    return df[["order_id", "country", "total"]]
```

!!! note "Types"
    agentl readers return CSV values as strings, like `pd.read_csv(dtype=str)`. Cast the columns you
    compute with, as above.

## 4. Write the pipeline

`pipeline.yaml`:

```yaml
name: sales
steps:
  - id: raw
    use: csv.read
    with:
      url: ${SALES_URL:-https://example.com/exports/sales.csv}

  - id: clean
    fn: steps:clean          # found next to the pipeline file
    input: raw

  - id: save
    use: postgres.write
    input: clean
    with:
      dsn: ${DATABASE_URL}   # never commit credentials
      table: sales
      mode: upsert
      key: order_id

notify:
  channels:
    console: {}
  rules:
    - on: [run.failed, source.empty]
      to: [console]
```

## 5. Run it

```bash
pip install 'agentl-etl[pandas,postgres]'
export DATABASE_URL=postgresql://user:pass@host:5432/db
agentl validate examples/sales/pipeline.yaml
agentl run examples/sales/pipeline.yaml --show save
agentl diagram examples/sales/pipeline.yaml
```

Run it twice: the table keeps one row per `order_id`, with the latest values.

## 6. What to do next

- Swap `console` for `slack` or `webhook` in `notify` ([Notifications](notifications.md)).
- Add an `agent:` step for the fuzzy parts (categorize, extract fields from free text)
  ([Agent steps](agents.md)).
- Schedule it with cron, CI or your orchestrator (see
  [#31](https://github.com/arimayuji/agentl/issues/31)).

## Checklist

- [ ] Each `read_*` became a reader connector (`csv.read`, `excel.read`, `file.read`, ...).
- [ ] Pure pandas transforms are `@dataframe` functions; nothing in them does I/O.
- [ ] Writes use `upsert` with a real key, so re-runs are safe.
- [ ] Secrets come from `${VAR}`, never from the YAML.
- [ ] `agentl validate` passes in CI.
