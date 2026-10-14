from agentl.pandas import dataframe

CALLS: list[str] = []


@dataframe
def clean(df):
    CALLS.append(type(df).__name__)
    df = df[df["kind"] == "a"].copy()
    df["amount"] = df["amount"].astype(float).round(1)
    df["missing"] = None
    return df


def plain(input):
    return [{**r, "seen": True} for r in input]
