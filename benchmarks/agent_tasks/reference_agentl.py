"""The ten tasks solved with agentl's Python API (`al.read`, `al.write`, `al.http_get_all`): proof
that the short way covers them, and the size of each answer next to `reference.py` (plain Python).
The tests run it as a stand-in agent, like `reference.py`."""

import re
import sys

import agentl as al

MARKERS = {
    "01": "orders.csv. Keep",
    "02": "customers.json has",
    "03": "customers.xlsx",
    "04": "/products",
    "05": "invoices/ has",
    "06": "pricing.html",
    "07": "shop.db",
    "08": "orders.json has",
    "09": "sales/ has",
    "10": "emails/ has",
    "11": "tickets.csv",
    "12": "reviews",
    "13": "/events",
}


def main() -> None:
    prompt = sys.stdin.read()
    task = next(k for k, m in MARKERS.items() if m in prompt)
    if task == "01":
        rows = [r for r in al.read("input/orders.csv") if r["status"] == "paid" and float(r["total"]) > 100]
        al.write(
            [{"id": int(r["id"]), "customer": r["customer"], "total": float(r["total"])} for r in rows],
            "out/result.json",
        )
    elif task == "02":
        names = {str(c["customer_id"]): c["name"] for c in al.read("input/customers.json")}
        orders = al.read("input/orders.csv")
        al.write(
            [
                {
                    "order_id": o["order_id"],
                    "customer_name": names.get(o["customer_id"], ""),
                    "total": o["total"],
                }
                for o in orders
            ],
            "out/result.csv",
        )
    elif task == "03":
        seen: dict = {}
        for r in al.read("input/customers.xlsx", sheet="export"):
            seen.setdefault(
                r["Cod Cliente"], {"id": r["Cod Cliente"], "name": r["Nome"], "email": r["E-mail"]}
            )
        al.write(list(seen.values()), "out/result.csv")
    elif task == "04":
        url = re.search(r"(http://127\.0\.0\.1:\d+)", prompt).group(1)  # type: ignore[union-attr]
        al.write(al.http_get_all(f"{url}/products", items="data", next="next_page"), "out/result.json")
    elif task == "05":
        rows = []
        for r in al.read("input/invoices/*.pdf"):
            number = re.search(r"INVOICE (\d+)", r["text"]).group(1)  # type: ignore[union-attr]
            total = re.search(r"R\$ ([\d.,]+)", r["text"]).group(1)  # type: ignore[union-attr]
            rows.append({"number": number, "total": total.replace(".", "").replace(",", ".")})
        al.write(rows, "out/result.csv")
    elif task == "06":
        rows = al.read("input/pricing.html")
        al.write(
            [{"plan": r["Plan"], "seats": r["Seats"], "price": r["Price"]} for r in rows], "out/result.csv"
        )
    elif task == "07":
        query = "select region, count(*) as orders, sum(total) as total from orders group by region"
        al.write(al.read("sqlite:///input/shop.db", query=query), "out/result.json")
    elif task == "08":
        rows = [
            {
                "id": o["id"],
                "customer_name": o["customer"]["name"],
                "city": o["customer"]["city"],
                "item_count": len(o["items"]),
                "quantity": sum(i["qty"] for i in o["items"]),
            }
            for o in al.read("input/orders.json")
        ]
        al.write(rows, "out/result.csv")
    elif task == "09":
        al.write(al.read("input/sales/*.csv"), "out/result.csv")
    elif task == "10":
        rows = []
        for r in al.read("input/emails/*.txt"):
            m = re.search(r"order #?(\d+)", r["text"])
            rows.append(
                {"file": r["path"].replace("\\", "/").rsplit("/", 1)[-1], "order": m.group(1) if m else None}
            )
        al.write(rows, "out/result.json")
    elif task in ("11", "12"):  # a script has no cache: every record to the LLM on every run
        from reference import ask_llm

        name, field = ("tickets", "category") if task == "11" else ("reviews", "sentiment")
        rows = []
        try:
            for r in al.read(f"input/{name}.csv"):
                rows.append({"id": r["id"], field: ask_llm(f"{field} of: {r['text']}")[field]})
        except Exception:
            pass  # the provider went down: keep what we have
        al.write(rows, "out/result.json")
    elif task == "13":
        url = re.search(r"(http://127\.0\.0\.1:\d+)", prompt).group(1)  # type: ignore[union-attr]
        events = al.read(f"{url}/events")
        al.write(events, "sqlite:///out/events.db", table="events", mode="replace")
    print('{"total_cost_usd": 0.0, "num_turns": 1, "usage": {"input_tokens": 10, "output_tokens": 5}}')


if __name__ == "__main__":
    main()
