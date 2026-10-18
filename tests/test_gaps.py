import json
import sqlite3
from pathlib import Path

from agentl.cli import main
from agentl.gaps import ExpectSpec, backfill_commands, expected, find
from agentl.spec import SpecError, parse


def _spec(**kw):
    return ExpectSpec.model_validate(kw)


def test_expected_days_skip_weekends_and_dates():
    e = _spec(
        partitions={"from": "2025-01-03", "to": "2025-01-08", "skip": ["weekends", "2025-01-07"]},
        present_in={"files": "x"},
    )
    got = [d.strftime("%Y-%m-%d") for d in expected(e.partitions)]
    assert got == ["2025-01-03", "2025-01-06", "2025-01-08"]


def test_expected_hours_and_since():
    e = _spec(
        partitions={"every": "1h", "from": "2025-01-01T00:00", "to": "2025-01-01T05:00"},
        present_in={"files": "x"},
    )
    assert len(expected(e.partitions)) == 6
    assert len(expected(e.partitions, since="2025-01-01T04:00")) == 2


def test_sqlite_gaps_and_ranges(tmp_path: Path):
    db = tmp_path / "out.db"
    con = sqlite3.connect(db)
    con.execute("create table facts (ref_date text)")
    con.executemany("insert into facts values (?)", [("2025-01-01",), ("2025-01-02",), ("2025-01-05",)])
    con.commit()
    con.close()
    e = _spec(
        partitions={"from": "2025-01-01", "to": "2025-01-06", "field": "ref_date"},
        present_in={
            "use": "sqlite.read",
            "with": {"db": str(db), "query": "select distinct ref_date from facts"},
        },
    )
    g = find(e)
    assert g.expected == 6
    assert [p["period"] for p in g.missing] == ["2025-01-03", "2025-01-04", "2025-01-06"]
    assert [(a, b) for _, a, b in g.ranges()] == [("2025-01-03", "2025-01-04"), ("2025-01-06", "2025-01-06")]
    assert backfill_commands("p.yaml", g)[0] == "agentl backfill p.yaml --from 2025-01-03 --to 2025-01-04"


def test_files_composite_keys(tmp_path: Path):
    for src, day in [("a", "2025-01-01"), ("a", "2025-01-02"), ("b", "2025-01-02")]:
        d = tmp_path / "out" / src / day
        d.mkdir(parents=True)
        (d / "part-0.csv").write_text("x\n", encoding="utf-8")
    e = _spec(
        partitions={"from": "2025-01-01", "to": "2025-01-02", "keys": {"source": ["a", "b"]}},
        present_in={"files": "out/{source}/{date:%Y-%m-%d}/*.csv"},
    )
    g = find(e, base_dir=str(tmp_path))
    assert g.expected == 4
    assert g.to_dict()["missing"] == [{"source": "b", "period": "2025-01-01"}]
    assert backfill_commands("p.yaml", g) == [
        "agentl backfill p.yaml --from 2025-01-01 --to 2025-01-01 --param source=b"
    ]


def test_spec_rejects_both_sources():
    import pytest

    with pytest.raises(SpecError):
        parse(
            {
                "name": "x",
                "steps": [{"id": "a", "use": "csv.read", "with": {"path": "a.csv"}}],
                "expect": {"partitions": {"from": "2025-01-01"}, "present_in": {"use": "a", "files": "b"}},
            }
        )


def test_cli_gaps(tmp_path: Path, capsys):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "2025-01-01.csv").write_text("x\n", encoding="utf-8")
    p = tmp_path / "p.yaml"
    p.write_text(
        "name: daily\n"
        "steps: [{id: a, use: csv.read, with: {path: a.csv}}]\n"
        "expect:\n"
        "  partitions: {every: 1d, from: '2025-01-01', to: '2025-01-03'}\n"
        "  present_in: {files: 'out/{date:%Y-%m-%d}.csv'}\n",
        encoding="utf-8",
    )
    assert main(["gaps", str(p), "--commands", "--fail"]) == 1
    out = capsys.readouterr().out
    assert "2 of 3 partitions missing" in out and "2025-01-02 .. 2025-01-03" in out
    assert "agentl backfill" in out
    assert main(["gaps", str(p), "--json", "--since", "2025-01-01", "--until", "2025-01-01"]) == 0
    assert json.loads(capsys.readouterr().out)["missing"] == []
