"""`python -m evals` runs end to end offline with the fake provider (#97)."""

import json

from evals import harness


def test_scoring_normalizes_fields():
    rows = [{"id": 1, "label": {"tax_id": "12.345.678/0001-90", "total": 10.5, "due_date": "2026-10-14"}}]
    ok = {1: {"tax_id": "12345678000190", "total": 10.50, "due_date": " 2026-10-14 "}}
    assert harness.score(rows, ok, ["tax_id", "total", "due_date"])["all_fields"] == 1.0
    bad = {1: {"tax_id": "12345678000190", "total": 10.49, "due_date": "2026-10-14"}}
    assert harness.score(rows, bad, ["tax_id", "total", "due_date"]) == {
        "all_fields": 0.0,
        "tax_id": 1.0,
        "total": 0.0,
        "due_date": 1.0,
    }


def test_harness_end_to_end_with_prices(tmp_path, capsys):
    out = tmp_path / "evals.json"
    args = ["--runs", "1", "--limit", "20", "--price-in", "1", "--price-out", "5", "--json", str(out)]
    assert harness.main(args) == 0
    report = json.loads(out.read_text())
    for task in ("tickets", "invoices"):
        v = report["tasks"][task]["variants"]
        assert v["agentl"]["accuracy"] == v["plain Python loop"]["accuracy"]  # same model, same answers
        assert v["agentl, records_per_call 10"]["calls"] == 2
        assert v["agentl, records_per_call 10"]["usd_per_1k_records"] < v["agentl"]["usd_per_1k_records"]
        cache = {c["changed"]: c for c in report["tasks"][task]["cache"]}
        assert cache[0.0]["calls_rerun"] == 0 and cache[0.1]["calls_rerun"] == 2
        assert abs(cache[0.5]["cost_saved"] - 0.5) < 0.05  # changed texts are a bit longer
    assert "they are not results" in capsys.readouterr().out


def test_datasets_are_well_formed():
    assert len(harness.load("tickets")) == 40 and len(harness.load("invoices")) == 40
    cats = {r["label"]["category"] for r in harness.load("tickets")}
    assert cats == {"billing", "bug", "account", "feature_request", "other"}
