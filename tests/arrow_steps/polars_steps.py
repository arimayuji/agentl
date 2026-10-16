import polars as pl

from agentl.polars import dataframe

CALLS: list[str] = []


@dataframe
def clean(df):
    CALLS.append(type(df).__name__)
    return df.filter(pl.col("kind") == "a").with_columns(
        pl.col("amount").cast(pl.Float64).round(1), pl.lit(None).alias("missing")
    )


@dataframe
def lazy(df):
    return df.lazy().filter(pl.col("kind") == "b").select("id", "kind")


@dataframe
def join(frames):
    return frames["left"].join(frames["right"], on="id")
