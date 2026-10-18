import json

import pytest

from agentl import testing
from agentl.cli import main
from agentl.spec import SpecError, load

PIPELINE = """
name: orders
llm:
  provider: anthropic
  model: some-model
steps:
  - id: orders
    use: postgres.read
    with: {dsn: "postgresql://nowhere/db", query: "select * from orders"}
  - id: big
    use: filter
    input: orders
    with: {where: {keep: "y"}}
  - id: tag
    agent: Copy the id.
    input: big
    output: {id: int, amount: int}
  - id: save
    use: postgres.write
    input: tag
    with: {dsn: "postgresql://nowhere/db", table: out}
notify:
  channels: {console: {}}
  rules: [{on: run.failed, to: [console]}]
"""


@pytest.fixture
def files(tmp_path):
    pipe = tmp_path / "p.yaml"
    pipe.write_text(PIPELINE, encoding="utf-8")
    fixture = tmp_path / "orders.csv"
    fixture.write_text("id,amount,keep\n1,5,n\n2,20,y\n3,30,y\n", encoding="utf-8")
    return tmp_path, pipe, fixture


def test_update_then_pass_then_fail(files, capsys):
    tmp, pipe, fixture = files
    golden = tmp / "golden.json"
    args = ["test", str(pipe), "--fixture", str(fixture), "--expect", str(golden), "--db", str(tmp / "s.db")]
    assert main([*args, "--update"]) == 0
    rows = json.loads(golden.read_text(encoding="utf-8"))
    assert [r["id"] for r in rows] == [2, 3]
    assert "_file" not in rows[0]
    assert main(args) == 0
    rows[1]["amount"] = 31
    rows.append({"id": 9, "amount": 99})
    golden.write_text(json.dumps(rows), encoding="utf-8")
    assert main(args) == 1
    err = capsys.readouterr().err
    assert "FAIL save: expected 3 record(s), got 2" in err
    assert "amount: expected 31, got 30" in err
    assert "missing row 2" in err


def test_named_fixture_and_key(files):
    tmp, pipe, fixture = files
    golden = tmp / "g.json"
    golden.write_text(
        json.dumps({"save": [{"id": 3, "amount": 30, "keep": "y"}, {"id": 2, "amount": 20, "keep": "y"}]}),
        encoding="utf-8",
    )
    spec = load(pipe)
    assert testing.run_test(spec, {"orders": str(fixture)}, golden, key="id").ok
    assert not testing.run_test(spec, {"orders": str(fixture)}, golden).ok


def test_source_without_fixture_is_an_error(files):
    _, pipe, _ = files
    with pytest.raises(SpecError, match="needs a fixture"):
        testing.prepare(load(pipe), {})


def test_prepare_replaces_writers_and_llm(files):
    _, pipe, fixture = files
    spec, compared = testing.prepare(load(pipe), {None: str(fixture)})
    assert compared == ["save"]
    assert spec.llm["default"].provider == "echo"
    assert spec.notify is None
    assert {s.id: s.use for s in spec.steps}["save"] == "concat"
