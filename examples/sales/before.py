"""The pandas script we start from (see docs/migrate-from-pandas.md)."""

import os

import pandas as pd
from sqlalchemy import create_engine

URL = "https://example.com/exports/sales.csv"

df = pd.read_csv(URL, sep=";")
df = df.dropna(subset=["order_id"]).drop_duplicates("order_id")
df["total"] = df["qty"].astype(int) * df["unit_price"].astype(float)
df["country"] = df["country"].str.strip().str.upper()

engine = create_engine(os.environ["DATABASE_URL"])
df.to_sql("sales", engine, if_exists="append", index=False)  # duplicates on re-run
