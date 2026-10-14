"""The pandas cleaning from the legacy script, unchanged, as an agentl step."""

import pandas as pd

from agentl.pandas import dataframe


@dataframe
def clean(dfs):
    df, cust = dfs["invoices"], dfs["customers"]
    if df.empty:
        return df
    df = df.dropna(subset=["number"]).drop_duplicates("number", keep="last")
    df["total"] = pd.to_numeric(df["total"], errors="coerce").fillna(0).round(2)
    df["issued_at"] = pd.to_datetime(df["issued_at"], errors="coerce", utc=True)
    df["tax_id"] = df["tax_id"].astype(str).str.replace(r"\D", "", regex=True)
    cust["tax_id"] = cust["tax_id"].astype(str).str.replace(r"\D", "", regex=True)
    df = df.merge(cust[["tax_id", "name", "segment"]], on="tax_id", how="left")
    df["customer_known"] = df["name"].notna()
    return df[(df["total"] > 0) & df["issued_at"].notna()]
