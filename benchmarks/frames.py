"""`@dataframe` step used by the Arrow benchmark (a module so `fn:` can find it)."""

from agentl.pandas import dataframe


@dataframe
def clean(df):
    df = df[df["kind"] == "a"].copy()
    df["amount"] = df["amount"].astype(float).round(1)
    return df


try:
    import polars as pl

    from agentl.polars import dataframe as polars_dataframe

    @polars_dataframe
    def clean_polars(df):
        return df.filter(pl.col("kind") == "a").with_columns(pl.col("amount").cast(pl.Float64).round(1))

except ImportError:  # polars is optional; the variant that uses it is skipped without it
    pass
