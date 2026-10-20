import json
import os
import sqlite3

import pytest

from agentl import diffing
from agentl.cli import main
from agentl.spec import load

PIPELINE = """
name: orders
steps:
  - id: orders
    use: csv.read
    with: {{path: '{csv}'}}
  - id: tag
    agent: Copy the fields.
    input: orders
    output: {{id: int, amount: int}}
  - id: save
    use: sqlite.write
    input: tag
    with: {{db: '{db}', table: out, mode: {mode}, key: id}}
  - id: dump
    use: json.write
    input: tag
    with: {{path: '{out}'}}
llm:
  provider: echo
  model: m
"""


def _setup(tmp_path, mode="upsert"):
    db = tmp_path / "dest.db"
    with sqlite3.connect(db) as c:
        c.execute("create table out (id, amount)")
        c.executemany("insert into out values (?, ?)", [(1, 5), (2, 20), (4, 40)])
    csv = tmp_path / "orders.csv"
    csv.write_text("id,amount\n1,5\n2,21\n3,30\n", encoding="utf-8")
    pipe = tmp_path / "p.yaml"
    out = tmp_path / "dump.json"
    pipe.write_text(PIPELINE.format(csv=csv, db=db, mode=mode, out=out), encoding="utf-8")
    return pipe, db, out


def _rows(db):
    with sqlite3.connect(db) as c:
        return c.execute("select id, amount from out order by id").fetchall()


def test_upsert_diff_writes_nothing(tmp_path):
    pipe, db, out = _setup(tmp_path)
    d = diffing.run_diff(load(pipe), store=str(tmp_path / "s.db"))
    assert d.status == "succeeded" and d.changed
    save, dump = d.writers
    assert save.counts == {"inserted": 1, "updated": 1, "deleted": 0, "unchanged": 1}
    changes = {r["id"]: r for r in save.rows}
    assert changes[3]["_change"] == "inserted"
    assert changes[2]["_change"] == "updated" and changes[2]["_before"] == {"amount": 20}
    assert "3 record(s)" in (dump.note or "")
    assert _rows(db) == [(1, 5), (2, 20), (4, 40)]
    assert not out.exists()
    assert not (tmp_path / "s.db").exists()  # the real store records nothing


def test_replace_reports_deletes(tmp_path):
    pipe, db, _ = _setup(tmp_path, mode="replace")
    d = diffing.run_diff(load(pipe))
    assert d.writers[0].counts["deleted"] == 1
    assert [r["id"] for r in d.writers[0].rows if r["_change"] == "deleted"] == [4]
    assert _rows(db) == [(1, 5), (2, 20), (4, 40)]


def test_append_without_key_is_not_diffed(tmp_path):
    pipe, db, _ = _setup(tmp_path)
    text = pipe.read_text(encoding="utf-8").replace(", key: id", "").replace("mode: upsert", "mode: append")
    pipe.write_text(text, encoding="utf-8")
    d = diffing.run_diff(load(pipe))
    assert "no diff without `key`" in (d.writers[0].note or "")
    assert not d.changed
    assert _rows(db) == [(1, 5), (2, 20), (4, 40)]


def test_cli_text_json_exit_code_and_fixture(tmp_path, capsys):
    pipe, _, _ = _setup(tmp_path)
    store = ["--db", str(tmp_path / "s.db")]
    assert main(["diff", str(pipe), *store]) == 0
    text = capsys.readouterr().out
    assert "save" in text and "+1 inserted, ~1 updated, -0 deleted, 1 unchanged" in text
    assert "amount: 20 -> 21" in text
    assert main(["diff", str(pipe), "--exit-code", *store]) == 1
    capsys.readouterr()
    same = tmp_path / "same.csv"
    same.write_text("id,amount\n1,5\n2,20\n", encoding="utf-8")
    assert main(["diff", str(pipe), "--fixture", f"orders={same}", "--exit-code", "--json", *store]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["changed"] is False and body["writers"][0]["unchanged"] == 2


@pytest.mark.skipif(not os.environ.get("AGENTL_TEST_PG_DSN"), reason="AGENTL_TEST_PG_DSN not set")
def test_postgres_writer(tmp_path):
    psycopg = pytest.importorskip("psycopg")
    dsn = os.environ["AGENTL_TEST_PG_DSN"]
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("drop table if exists agentl_diff_out")
        c.execute("create table agentl_diff_out (id bigint primary key, amount bigint)")
        c.execute("insert into agentl_diff_out values (1, 5), (2, 20)")
    csv = tmp_path / "o.csv"
    csv.write_text("id,amount\n1,5\n2,21\n3,30\n", encoding="utf-8")
    pipe = tmp_path / "p.yaml"
    pipe.write_text(
        f"""
name: pg
steps:
  - id: orders
    use: csv.read
    with: {{path: '{csv}'}}
  - id: save
    use: postgres.write
    input: orders
    with: {{dsn: '{dsn}', table: agentl_diff_out, mode: upsert, key: id}}
""",
        encoding="utf-8",
    )
    d = diffing.run_diff(load(pipe))
    assert d.status == "succeeded", d.error
    assert d.writers[0].counts == {"inserted": 1, "updated": 1, "deleted": 0, "unchanged": 1}
    with psycopg.connect(dsn, autocommit=True) as c:
        assert c.execute("select id, amount from agentl_diff_out order by id").fetchall() == [(1, 5), (2, 20)]
        c.execute("drop table agentl_diff_out")
