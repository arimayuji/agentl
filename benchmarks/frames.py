"""`@dataframe` step used by the Arrow benchmark (a module so `fn:` can find it)."""

from agentl.pandas import dataframe


@dataframe
def clean(df):
    df = df[df["kind"] == "a"].copy()
    df["amount"] = df["amount"].astype(float).round(1)
    return df
