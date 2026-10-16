"""Bridge between agentl records and polars DataFrames.

Install with `pip install 'agentl-etl[polars]'`.

    import polars as pl
    from agentl.polars import dataframe

    @dataframe
    def clean(df, min_total=0):
        return df.filter(pl.col("total") >= min_total).with_columns(pl.col("total").round(2))

Use it in YAML as `fn: mypkg.steps:clean`. Steps with several inputs receive a dict of
DataFrames keyed by step id. With pyarrow installed the runner passes Arrow tables in and out, and
polars wraps them without copying."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from .registry import require


def to_df(records: list[dict] | None) -> Any:
    """Records -> polars DataFrame (columns in first-seen order)."""
    pl = require("polars", "polars")
    return pl.DataFrame(records or [], infer_schema_length=None)


def to_records(df: Any) -> list[dict]:
    """polars DataFrame (or Series, or LazyFrame) -> records with plain Python values."""
    return [dict(r) for r in _collect(df).to_dicts()]


def to_table(df: Any) -> Any:
    """polars DataFrame -> pyarrow Table with the same columns and values `to_records` would give."""
    pa = require("pyarrow", "arrow")
    table = _collect(df).to_arrow()
    # polars hands out large_string / string_view; records would give string.
    fields = [
        pa.field(
            f.name,
            pa.string() if pa.types.is_large_string(f.type) or pa.types.is_string_view(f.type) else f.type,
            f.nullable,
        )
        for f in table.schema
    ]
    return table.cast(pa.schema(fields))


ENGINE_KEY = b"agentl.engine"


def tag(table: Any) -> Any:
    """Mark an Arrow table as a polars step's output, in polars' types (see `from_polars`)."""
    return table.replace_schema_metadata({ENGINE_KEY: b"polars"})


def untag(table: Any) -> Any:
    """A tagged table as `to_table` would give it: string columns as `string`, no tag."""
    return to_table(require("polars", "polars").from_arrow(table.replace_schema_metadata(None)))


def from_polars(table: Any) -> bool:
    """Whether an Arrow table came out of a polars step."""
    schema = getattr(table, "schema", None)
    meta = getattr(schema, "metadata", None) or {}
    return bool(meta.get(ENGINE_KEY) == b"polars")


def write_parquet(table: Any, path: str, compression: str) -> None:
    """Write a polars step's table with polars' own Parquet writer: several times faster than
    pyarrow's on a fresh process, whose allocator warm-up dominates a sub-second job. Strings come
    back as polars writes them (large_string when read with pyarrow); values are the same."""
    pl = require("polars", "polars")
    pl.from_arrow(table.replace_schema_metadata(None)).write_parquet(path, compression=compression)


def _collect(df: Any) -> Any:
    pl = require("polars", "polars")
    if isinstance(df, pl.LazyFrame):
        df = df.collect()
    if isinstance(df, pl.Series):
        df = df.to_frame()
    if not isinstance(df, pl.DataFrame):
        raise TypeError(f"expected a polars DataFrame, got {type(df).__name__}")
    return df


def _frame(value: Any) -> Any:
    """Records or a pyarrow Table -> polars DataFrame (a Table is wrapped without copying)."""
    if hasattr(value, "to_pandas") and hasattr(value, "num_rows"):
        pl = require("polars", "polars")
        return pl.from_arrow(value)
    return to_df(value)


def dataframe(fn: Callable[..., Any]) -> Callable[..., list[dict]]:
    """Wrap `fn(df, **params) -> DataFrame` (polars) as an agentl step `fn(records, **params) ->
    records`. It may also return a LazyFrame, which is collected.

    With `agentl-etl[arrow]` the runner may hand the step a pyarrow Table instead of records and
    ask for a Table back (`_arrow=True`), skipping the per-row conversion on both sides."""

    @functools.wraps(fn)
    def step(input: Any = None, _arrow: bool = False, **params: Any) -> Any:
        if isinstance(input, dict):
            arg: Any = {k: _frame(v) for k, v in input.items()}
        else:
            arg = _frame(input)
        out = fn(arg, **params)
        if out is None:
            out = to_df([])
        if _arrow:
            # Tagged and in polars' own types: the Arrow writers turn it back to polars (Parquet)
            # or normalise it (Postgres), so no cast here, and no pyarrow.compute import.
            return tag(_collect(out).to_arrow())
        return to_records(out)

    step.agentl_dataframe = True  # type: ignore[attr-defined]
    step.agentl_engine = "polars"  # type: ignore[attr-defined]
    return step
