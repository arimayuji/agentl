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


FILL = """name: daily
params:
  day: date
steps:
  - {id: raw, use: csv.read, with: {path: in.csv}}
  - {id: out, use: json.write, input: raw, with: {path: 'out/${params.day}.json'}}
expect:
  partitions: {every: 1d, from: '2025-01-01', to: '2025-01-04'}
  present_in: {files: 'out/{date:%Y-%m-%d}.json'}
notify:
  channels: {ops: {type: console}}
"""


def _fill_pipe(tmp_path: Path, monkeypatch, text: str = FILL) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "in.csv").write_text("id\n1\n", encoding="utf-8")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "2025-01-02.json").write_text("[]", encoding="utf-8")
    p = tmp_path / "p.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_cli_gaps_fill_runs_only_missing(tmp_path: Path, monkeypatch, capsys):
    p = _fill_pipe(tmp_path, monkeypatch)
    db = str(tmp_path / "runs.db")
    assert main(["gaps", str(p), "--fill", "--max", "2", "--fail", "--db", db]) == 1  # one deferred
    out = capsys.readouterr().out
    assert "3 of 4 partitions missing" in out and "over --max 2" in out
    assert sorted(f.name for f in (tmp_path / "out").iterdir()) == [
        "2025-01-01.json",
        "2025-01-02.json",
        "2025-01-03.json",
    ]
    assert main(["gaps", str(p), "--fill", "--json", "--db", db]) == 0
    res = json.loads(capsys.readouterr().out)
    assert [m["period"] for m in res["missing"]] == ["2025-01-04"]
    assert res["fill"]["backfills"][0]["succeeded"] == 1 and res["fill"]["deferred"] == []
    assert main(["gaps", str(p), "--fail"]) == 0


def test_fill_composite_keys_pass_params(tmp_path: Path, monkeypatch):
    text = (
        FILL.replace("  day: date\n", "  day: date\n  source: str\n")
        .replace("out/${params.day}.json", "out/${params.source}-${params.day}.json")
        .replace("to: '2025-01-04'", "to: '2025-01-02', keys: {source: [a, b]}")
        .replace("out/{date:%Y-%m-%d}.json", "out/{source}-{date:%Y-%m-%d}.json")
    )
    p = _fill_pipe(tmp_path, monkeypatch, text)
    (tmp_path / "out" / "a-2025-01-01.json").write_text("[]", encoding="utf-8")
    from agentl.gaps import fill, load_spec

    spec = load_spec(str(p))
    assert spec.expect is not None
    g = find(spec.expect, base_dir=str(tmp_path))
    res = fill(str(p), g, max_runs=None, db=str(tmp_path / "runs.db"))
    assert res.ok and [r.total for r in res.reports] == [1, 2]
    assert find(spec.expect, base_dir=str(tmp_path)).missing == []


def test_fill_max_zero_runs_nothing(tmp_path: Path, monkeypatch):
    p = _fill_pipe(tmp_path, monkeypatch)
    from agentl.gaps import fill, load_spec

    spec = load_spec(str(p))
    assert spec.expect is not None
    res = fill(str(p), find(spec.expect, base_dir=str(tmp_path)), max_runs=0)
    assert res.reports == [] and len(res.deferred) == 3


def test_cli_gaps_notify(tmp_path: Path, monkeypatch, capsys):
    p = _fill_pipe(tmp_path, monkeypatch)
    sent = []
    from agentl import notify

    monkeypatch.setitem(notify.CHANNELS, "console", lambda cfg, text, event: sent.append((text, event)))
    assert main(["gaps", str(p), "--notify", "ops"]) == 0
    assert sent and "3 of 4 partitions missing" in sent[0][0] and sent[0][1]["type"] == "gaps.found"
    assert main(["gaps", str(p), "--notify", "nope"]) == 2  # unknown channel
