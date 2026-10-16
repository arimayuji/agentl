# Extending

## Connectors

A connector is a function `fn(input, **params) -> list[dict]`:

- `input` is `None` for sources, a list of records for one upstream step, or `{step_id: records}`
  when the step has several inputs.
- `params` come from the step's `with:` block.

```python
from agentl import connector


@connector("crm.contacts")
def contacts(input, since=None):
    return [{"id": 1, "email": "a@b.com"}]
```

### Chunked execution

With `chunk_size` (see [Large inputs](concepts.md#large-inputs-chunk_size)), the runner streams a
chain only through connectors that say they can. Add these next to the normal function, which
stays the one used without `chunk_size`:

```python
from agentl.registry import rowwise, stream_reader, stream_writer


@stream_reader("crm.contacts")
def contacts_chunks(input, chunk_size, since=None):
    for page in fetch_pages(since, page_size=chunk_size):
        yield page  # a list of records per chunk


@stream_writer("crm.upsert")
def upsert_chunks(chunks, list_id):
    n = 0
    for rows in chunks:  # one iterator of lists, in one session or transaction
        send(list_id, rows)
        n += len(rows)
    return [{"written": n}]


rowwise("crm.enrich")  # same answer per chunk as on the whole input
```

### Publishing a connector package

Register through the `agentl.connectors` entry point group so users only need to install it:

```toml
[project.entry-points."agentl.connectors"]
"crm.contacts" = "agentl_crm:contacts"
```

Please name packages `agentl-<name>`.

## LLM providers

A provider factory receives the `LLMSpec` and returns an object with two methods:

```python
from agentl import register_provider
from agentl.llm import Result


class MyProvider:
    def __init__(self, spec):
        self.spec = spec

    def structured(self, instruction, record, schema):
        data = ...  # call your model, get a dict
        return Result(schema.model_validate(data), input_tokens=0, output_tokens=0)

    def text(self, instruction, content):
        return "..."


register_provider("mine", MyProvider)
```

## Functions

Any importable function works as a step:

```yaml
- id: clean
  fn: my_package.cleaning:normalize
  input: raw
```

It receives the input records plus the `with:` parameters as keyword arguments.

`my_package.cleaning` is looked up in this order:

1. a file next to the pipeline YAML (`my_package/cleaning.py`), loaded under a name unique to that
   file, so two pipelines with their own `steps.py` never share code, even in one worker process;
2. a normal import (installed packages and `sys.path`);
3. a file in the current working directory.

### pandas steps

Already have pandas code? Wrap it with `@dataframe` (`pip install 'agentl-etl[pandas]'`):

```python
from agentl.pandas import dataframe


@dataframe
def clean(df, min_total=0):
    df = df.astype({"total": float})
    return df[df.total >= min_total].drop_duplicates("id")
```

```yaml
- id: clean
  fn: my_package.steps:clean
  input: raw
  with: {min_total: 10}
```

The step receives a DataFrame (a dict of DataFrames keyed by step id when it has several inputs)
and returns a DataFrame or Series. JSON values keep their types; CSV values are strings. Results go back to records with plain Python values: NaN, NaT and
NA become `None`, numpy scalars become `int`/`float`, timestamps become `datetime`, and a
non-default index (for example after `groupby`) becomes columns. `agentl.pandas.to_df` and
`to_records` are available for manual conversion.

With `agentl-etl[arrow]` installed, a `@dataframe` step next to an Arrow-capable reader or writer
gets its DataFrame straight from a pyarrow Table and hands one back, without building records in
between. See [Columnar fast path](concepts.md#columnar-fast-path-arrow).

Several inputs:

```python
@dataframe
def revenue(dfs):
    orders = dfs["orders"].astype({"amount": float})
    df = orders.merge(dfs["users"], left_on="user_id", right_on="id")
    return df.groupby("country", as_index=False)["amount"].sum()
```

```yaml
- id: revenue
  fn: steps:revenue
  input: [orders, users]
```

### polars

The same with polars (`pip install 'agentl-etl[polars]'`):

```python
import polars as pl
from agentl.polars import dataframe


@dataframe
def clean(df, min_total=0):
    return df.with_columns(pl.col("total").cast(pl.Float64)).filter(pl.col("total") >= min_total)
```

The step receives a polars DataFrame (a dict of them with several inputs) and may return a
DataFrame, a Series or a LazyFrame, which is collected. Next to an Arrow-capable reader or writer
the Table is wrapped without copying, both ways. On the CSV → Parquet job in
[Performance](performance.md) a polars step is about 1.5x faster than the same step in pandas;
reading and writing are still pyarrow's, which is why plain polars is faster still (#171).
