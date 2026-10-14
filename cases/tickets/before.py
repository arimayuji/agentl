"""Support ticket triage, by hand: read the CSV, ask the model about each ticket, save an Excel report."""

import pandas as pd

from cases.fake_llm import complete  # stands in for client.messages.create(...) plus parsing the answer


def main(work: str) -> None:
    df = pd.read_csv(f"{work}/data/tickets.csv")
    answers = [complete("tickets", text) for text in df["text"]]
    df["category"] = [a["category"] for a in answers]
    df["urgent"] = [a["urgent"] for a in answers]
    df.to_excel(f"{work}/out/triage.xlsx", index=False)
