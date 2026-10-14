"""Builds data/invoices.jsonl: invoice texts written in different styles, with the fields to
extract as labels. Synthetic and deterministic (seed 7): run it again and you get the same file.

    python -m evals.make_invoices"""

from __future__ import annotations

import datetime as dt
import json
import random
from pathlib import Path

SUPPLIERS = [
    "Rio Dados Ltda",
    "Copacabana Cloud SA",
    "Lagoa Analytics ME",
    "Leblon Software Ltda",
    "Urca Labs SA",
]
MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def _cnpj(r: random.Random) -> str:
    d = [r.randint(0, 9) for _ in range(12)]
    return f"{d[0]}{d[1]}.{d[2]}{d[3]}{d[4]}.{d[5]}{d[6]}{d[7]}/0001-{r.randint(10, 99)}"


def _brl(v: float) -> str:
    whole, cents = f"{v:,.2f}".split(".")
    return f"R$ {whole.replace(',', '.')},{cents}"


def _date(d: dt.date, style: int) -> str:
    if style == 0:
        return d.strftime("%d/%m/%Y")
    if style == 1:
        return f"{MONTHS[d.month - 1]} {d.day}, {d.year}"
    return d.isoformat()


TEMPLATES = [
    "INVOICE {num}\nSupplier: {supplier} (CNPJ {cnpj})\nServices for {month}.\n"
    "Total due: {money}\nDue date: {due}",
    "{supplier} - CNPJ: {cnpj}\nInvoice no. {num}, issued {issued}.\n"
    "Please pay {money} by {due}. Late payments incur 2% interest.",
    "Hi team, attached is invoice {num} from {supplier}. Amount {money}, due on {due}. "
    "Our tax id is {cnpj}. "
    "The previous invoice of {old} was already paid, thanks!",
    "NF {num} | {supplier} | tax id {cnpj} | issued {issued} | due {due} | "
    "subtotal {sub} | taxes {tax} | total {money}",
]


def build(n: int = 40, seed: int = 7) -> list[dict]:
    r = random.Random(seed)
    rows = []
    for i in range(1, n + 1):
        issued = dt.date(2026, 1, 1) + dt.timedelta(days=r.randint(0, 270))
        due = issued + dt.timedelta(days=r.choice([15, 30, 45]))
        sub = round(r.uniform(300, 25000), 2)
        tax = round(sub * 0.1125, 2)
        total = round(sub + tax, 2)
        style = r.randint(0, 2)
        cnpj = _cnpj(r)
        text = TEMPLATES[(i - 1) % len(TEMPLATES)].format(
            num=f"{2026}-{i:04d}",
            supplier=r.choice(SUPPLIERS),
            cnpj=cnpj,
            month=MONTHS[issued.month - 1],
            money=_brl(total),
            due=_date(due, style),
            issued=_date(issued, style),
            sub=_brl(sub),
            tax=_brl(tax),
            old=_brl(round(r.uniform(300, 25000), 2)),
        )
        rows.append(
            {"id": i, "text": text, "label": {"tax_id": cnpj, "total": total, "due_date": due.isoformat()}}
        )
    return rows


if __name__ == "__main__":
    out = Path(__file__).parent / "data" / "invoices.jsonl"
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in build()), encoding="utf-8")
    print(f"wrote {out}")
