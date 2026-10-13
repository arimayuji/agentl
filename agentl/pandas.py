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


def dataframe(fn: Callable[..., Any]) -> Callable[..., list[dict]]:
    """Wrap `fn(df, **params) -> DataFrame` as an agentl step `fn(records, **params) -> records`."""

    @functools.wraps(fn)
    def step(input: Any = None, **params: Any) -> list[dict]:
        if isinstance(input, dict):
            arg: Any = {k: to_df(v) for k, v in input.items()}
        else:
            arg = to_df(input)
        out = fn(arg, **params)
        return [] if out is None else to_records(out)

    return step
