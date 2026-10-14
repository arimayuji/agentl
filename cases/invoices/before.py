"""Invoice extraction, by hand: read each invoice text, ask the model for the fields, load SQLite."""

import glob
import sqlite3

from cases.fake_llm import complete  # stands in for client.messages.create(...) plus parsing the answer


def main(work: str) -> None:
    rows = []
    for path in sorted(glob.glob(f"{work}/data/invoices/*.txt")):
        with open(path, encoding="utf-8") as f:
            fields = complete("invoices", f.read())
        rows.append((path, fields["tax_id"], fields["total"], fields["due_date"]))
    conn = sqlite3.connect(f"{work}/out/finance.db")
    with conn:
        conn.execute("DROP TABLE IF EXISTS invoices")
        conn.execute("CREATE TABLE invoices (path, tax_id, total, due_date)")
        conn.executemany("INSERT INTO invoices VALUES (?, ?, ?, ?)", rows)
    conn.close()
