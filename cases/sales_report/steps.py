from agentl.pandas import dataframe


@dataframe
def by_region(df):
    df["amount"] = df["amount"].astype(float)
    report = df.groupby("region", as_index=False)["amount"].sum().rename(columns={"amount": "total"})
    report["total"] = report["total"].round(2)
    return report
