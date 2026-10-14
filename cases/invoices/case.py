"""Extract fields from invoice texts with an LLM and load them into a database."""

import json
import sqlite3
from pathlib import Path

TITLE = "Invoice field extraction"
NEEDS: tuple[str, ...] = ()
OUTPUT = "out/finance.db"


def make_data(work: Path) -> None:
    (work / "data/invoices").mkdir()
    for line in (Path(__file__).parents[2] / "evals/data/invoices.jsonl").read_text().splitlines():
        r = json.loads(line)
        (work / f"data/invoices/inv_{r['id']:03d}.txt").write_text(r["text"], encoding="utf-8")


def result(work: Path) -> list:
    conn = sqlite3.connect(work / OUTPUT)
    rows = conn.execute("SELECT path, tax_id, total, due_date FROM invoices").fetchall()
    conn.close()
    return sorted((Path(p).name, t, round(float(v), 2), d) for p, t, v, d in rows)
