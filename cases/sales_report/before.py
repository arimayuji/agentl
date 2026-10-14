"""Daily sales report, by hand: clean the export, check it, total by region, save a dated Excel
file plus a `latest` copy in the shared folder, and keep the last 7 versions."""

import datetime as dt
import glob
import os
import shutil

import pandas as pd


def main(work: str) -> None:
    df = pd.read_csv(f"{work}/data/sales.csv", dtype=str, keep_default_na=False)
    df = df[df["amount"] != ""].drop_duplicates(subset="order_id")
    if len(df) < 10 or (df["region"] == "").any():
        raise ValueError(f"sales export looks wrong: {len(df)} rows")
    df["amount"] = df["amount"].astype(float)
    report = df.groupby("region", as_index=False)["amount"].sum().rename(columns={"amount": "total"})
    report["total"] = report["total"].round(2)
    path = f"{work}/out/sales_{dt.date.today().isoformat()}.xlsx"
    report.to_excel(path, index=False)
    shutil.copyfile(path, f"{work}/out/sales_latest.xlsx")
    for old in sorted(glob.glob(f"{work}/out/sales_????-??-??.xlsx"))[:-7]:
        os.remove(old)
