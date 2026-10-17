"""agentl validate / lint / schema (#118)."""

import json

from agentl import lint
from agentl.cli import main

BROKEN = """name: tickets
llm:
  default: { provider: echo }
steps:
  - id: read
    use: csv.reed
    with: { path: tickets.csv }
  - id: classify
    input: raed
    agent: Classify.
    llm: defualt
    output:
      category: str?
  - id: read
    use: json.write
    input: classify
    with: { path: "${OUT_DIR}/x.json" }
"""


def codes(report):
    return [(f.code, f.line, f.col) for f in report.findings]


def test_every_error_at_once_with_position_and_fix():
    r = lint.check_text(BROKEN, env={})
    assert codes(r) == [
        ("AE003", 6, 10),
        ("AE004", 9, 12),
        ("AE005", 11, 10),
        ("AE006", 14, 9),
        ("AE008", 17, 20),
    ]
    by = {f.code: f for f in r.findings}
    assert "Did you mean 'csv.read'?" in by["AE003"].message and by["AE003"].fix == "use: csv.read"
    assert by["AE004"].fix == "input: read" and by["AE005"].fix == "llm: default"
    assert r.spec is None


def test_yaml_errors_have_a_position():
    r = lint.check_text("name: x\nsteps:\n  - id: a\n    output: { a: str? }\n")
    assert r.findings[0].code == "AE001" and r.findings[0].line == 4 and "block style" in r.findings[0].fix


def test_structure_errors_point_at_the_step():
    r = lint.check_text("name: x\nsteps:\n  - id: a\n  - id: b\n    use: select\n    nope: 1\n")
    assert [(f.code, f.line) for f in r.findings] == [("AE002", 3), ("AE002", 6)]
    assert "unknown field 'nope'" in r.findings[1].message


GOOD_BUT = """name: sync
budjet: { max_usd: 1 }
llm:
  default: { provider: openai, model: gpt-4o-mini, options: { api_key: sk-abcdefghijklmnopqrstuvwxyz12 } }
steps:
  - id: rows
    use: csv.read
    with: { path: in.csv }
    cursor: { field: updated_at }
  - id: tag
    input: rows
    agent: Tag it.
    output: { tag: str }
  - id: spare
    use: select
    input: rows
  - id: run_code
    fn: missing_module:go
    input: tag
  - id: save
    use: postgres.write
    input: tag
    with: { dsn: "postgresql://u:hunter2@db/x", table: t }   # agentl: disable=AL003
"""


def test_lint_warnings():
    r = lint.check_text(GOOD_BUT, base_dir="/nonexistent")
    assert not r.errors and r.spec is not None
    assert [f.code for f in r.warnings] == ["AL007", "AL003", "AL006", "AL001", "AL004", "AL005", "AL002"]
    assert "Did you mean 'budget'?" in r.warnings[0].message
    assert "hunter2" not in r.render("json") and "sk-abc" not in r.render("text")


def test_lint_rules_can_be_disabled():
    text = GOOD_BUT.replace("name: sync\n", "name: sync\nlint: { disable: [AL001, AL007] }\n")
    assert {f.code for f in lint.check_text(text, base_dir="/x").warnings} == {
        "AL002",
        "AL003",
        "AL004",
        "AL005",
        "AL006",
    }


def test_cli_formats_and_exit_codes(tmp_path, capsys):
    p = tmp_path / "p.yaml"
    p.write_text(GOOD_BUT)
    assert main(["validate", str(p)]) == 0
    assert "ok: sync (5 steps)" in capsys.readouterr().out
    assert main(["lint", str(p)]) == 0 and "AL002" in capsys.readouterr().out
    assert main(["lint", str(p), "--strict"]) == 1
    capsys.readouterr()
    assert main(["lint", str(p), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] and {f["code"] for f in data["findings"]} >= {"AL001", "AL002"}
    assert main(["lint", str(p), "--format", "github"]) == 0
    assert "::warning file=" in capsys.readouterr().out
    bad = tmp_path / "bad.yaml"
    bad.write_text(BROKEN.replace("${OUT_DIR}", "out"))
    assert main(["validate", str(bad), "--format", "json"]) == 2
    assert not json.loads(capsys.readouterr().out)["ok"]


def test_schema_covers_the_shortcuts(capsys):
    assert main(["schema"]) == 0
    s = json.loads(capsys.readouterr().out)
    props = s["properties"]
    assert {"name", "steps", "llm", "budget", "extract", "lint"} <= set(props)
    step = s["$defs"]["StepSpec"]["properties"]
    assert "with" in step and "cursor" in step and "anyOf" in step["llm"]


def test_examples_have_no_structure_errors():
    from pathlib import Path

    for p in Path("examples").rglob("*.yaml"):
        report = lint.check_file(p, env={"SLACK_WEBHOOK": "x", "DATABASE_URL": "x", "INVOICES_DSN": "x"})
        assert not [f for f in report.errors if f.code != "AE008"], (p, report.render())


def test_cast_without_locale_on_en_us_numbers_warns(tmp_path):
    (tmp_path / "prices.csv").write_text('id,price\n1,"1,234.50"\n2,3.10\n', encoding="utf-8")
    pipe = """name: p
steps:
  - id: read
    use: csv.read
    with: { path: prices.csv }
  - id: clean
    input: read
    use: cast
    with:
      fields: { price: decimal }
"""
    (tmp_path / "p.yaml").write_text(pipe, encoding="utf-8")
    codes = [f.code for f in lint.check_file(tmp_path / "p.yaml").findings]
    assert "AL010" in codes
    (tmp_path / "p.yaml").write_text(
        pipe.replace("    with:\n      fields", "    with:\n      locale: en-US\n      fields"),
        encoding="utf-8",
    )
    assert "AL010" not in [f.code for f in lint.check_file(tmp_path / "p.yaml").findings]
