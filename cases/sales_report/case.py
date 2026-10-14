"""Clean a daily export, check it, total by region and publish a versioned spreadsheet. No LLM."""

import csv
import random
from pathlib import Path

TITLE = "Daily sales report to a shared folder"
NEEDS: tuple[str, ...] = ("pandas", "openpyxl")
OUTPUT = "out/sales_latest.xlsx"


def make_data(work: Path) -> None:
    r = random.Random(3)
    with open(work / "data/sales.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["order_id", "region", "amount", "date"])
        for i in range(2000):
            amount = "" if i % 50 == 0 else f"{r.uniform(10, 900):.2f}"
            w.writerow([i, r.choice(["north", "south", "east", "west"]), amount, "2026-10-14"])
            if i % 97 == 0:  # the export repeats some orders
                w.writerow([i, r.choice(["north", "south"]), amount, "2026-10-14"])


def result(work: Path) -> list:
    import openpyxl

    ws = openpyxl.load_workbook(work / OUTPUT).active
    return sorted((r[0].value, round(float(r[1].value), 2)) for r in ws.iter_rows(min_row=2))
