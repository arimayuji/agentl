"""Bridge between agentl records and pandas DataFrames.

Install with `pip install 'agentl-etl[pandas]'`.

    from agentl.pandas import dataframe

    @dataframe
    def clean(df, min_total=0):
        return df[df.total >= min_total].assign(total=lambda d: d.total.round(2))

Use it in YAML as `fn: mypkg.steps:clean`. Steps with several inputs receive a dict of
DataFrames keyed by step id."""

from __future__ import annotations

import functools
import math
from collections.abc import Callable
from typing import Any

from .registry import require


def to_df(records: list[dict] | None) -> Any:
    """Records -> DataFrame (columns in first-seen order)."""
    pd = require("pandas", "pandas")
    return pd.DataFrame.from_records(records or [])


def _clean(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if hasattr(v, "item") and type(v).__module__ == "numpy":
        return _clean(v.item())  # numpy scalar -> python
    if type(v).__name__ in ("NaTType", "NAType"):
        return None
    if type(v).__name__ == "Timestamp":
        return v.to_pydatetime()
    return v


def to_records(df: Any) -> list[dict]:
    """DataFrame (or Series) -> records with plain Python values; NaN/NaT/NA become None."""
    pd = require("pandas", "pandas")
    if isinstance(df, pd.Series):
        df = df.to_frame()
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"expected a DataFrame, got {type(df).__name__}")
    if not isinstance(df.index, pd.RangeIndex):
        df = df.reset_index()
    cols = [str(c) for c in df.columns]
    return [
        {c: _clean(v) for c, v in zip(cols, row, strict=True)}
        for row in df.itertuples(index=False, name=None)
    ]


def to_table(df: Any) -> Any:
    """DataFrame -> pyarrow Table with the same columns and values `to_records` would give."""
    pd = require("pandas", "pandas")
    pa = require("pyarrow", "arrow")
    if isinstance(df, pd.Series):
        df = df.to_frame()
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"expected a DataFrame, got {type(df).__name__}")
    if not isinstance(df.index, pd.RangeIndex):
        df = df.reset_index()
    df = df.set_axis([str(c) for c in df.columns], axis=1)
    table = pa.Table.from_pandas(df, preserve_index=False).replace_schema_metadata(None)
    # pandas' string columns come out as large_string; records would give string.
    fields = [
        pa.field(f.name, pa.string() if pa.types.is_large_string(f.type) else f.type, f.nullable)
        for f in table.schema
    ]
    return table.cast(pa.schema(fields))


def _frame(value: Any) -> Any:
    """Records or a pyarrow Table -> DataFrame."""
    if hasattr(value, "to_pandas") and hasattr(value, "num_rows"):
        return value.to_pandas()
    return to_df(value)


def dataframe(fn: Callable[..., Any]) -> Callable[..., list[dict]]:
    """Wrap `fn(df, **params) -> DataFrame` as an agentl step `fn(records, **params) -> records`.

    With `agentl-etl[arrow]` the runner may hand the step a pyarrow Table instead of records and
    ask for a Table back (`_arrow=True`), skipping the per-row conversion on both sides."""

    @functools.wraps(fn)
    def step(input: Any = None, _arrow: bool = False, **params: Any) -> Any:
        if isinstance(input, dict):
            arg: Any = {k: _frame(v) for k, v in input.items()}
        else:
            arg = _frame(input)
        out = fn(arg, **params)
        if _arrow:
            return to_table(to_df([]) if out is None else out)
        return [] if out is None else to_records(out)

    step.agentl_dataframe = True  # type: ignore[attr-defined]
    return step
