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
and returns a DataFrame or Series. Results go back to records with plain Python values: NaN, NaT and
NA become `None`, numpy scalars become `int`/`float`, timestamps become `datetime`, and a
non-default index (for example after `groupby`) becomes columns. `agentl.pandas.to_df` and
`to_records` are available for manual conversion.
