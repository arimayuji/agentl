"""pandas logic reused as an agentl step."""

from agentl.pandas import dataframe


@dataframe
def clean(df):
    df = df.replace("", None)  # CSV readers give empty strings, pandas expects NaN
    df = df.dropna(subset=["order_id"]).drop_duplicates("order_id")
    df["order_id"] = df["order_id"].astype(int)
    df["total"] = df["qty"].astype(int) * df["unit_price"].astype(float)
    df["country"] = df["country"].str.strip().str.upper()
    return df[["order_id", "country", "total"]]
