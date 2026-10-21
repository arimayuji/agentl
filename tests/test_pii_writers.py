"""Personal data before a destination (#239): `agentl preview` lists suspected fields and the
`protect` step for them; lint AL014 warns when a writer gets them unprotected, unless the step
says `sensitive: false` or a `pii` check runs before it. No value is ever echoed."""

import json

import pytest
import yaml

from agentl import lint, parse
from agentl.cli import main
from agentl.preflight import preview
from agentl.spec import SpecError
from agentl.store import Store

EMAIL, CPF = "ana@example.com", "529.982.247-25"
CSV = f"id,email,tax_id,city\n1,{EMAIL},{CPF},Recife\n2,bo@example.org,,Natal\n"

PIPE = """
name: w
steps:
  - id: src
    use: csv.read
    with: {{ path: in.csv }}
{middle}  - id: save
    use: json.write
    input: {last}
    with: {{ path: out.json }}
{extra}"""


def text(middle="", last="src", extra=""):
    return PIPE.format(middle=middle, last=last, extra=extra)


def al014(tmp_path, yaml_text):
    (tmp_path / "in.csv").write_text(CSV)
    return [f for f in lint.check_text(yaml_text, base_dir=str(tmp_path)).findings if f.code == "AL014"]


def test_lint_warns_when_a_writer_gets_personal_data(tmp_path):
    hit = al014(tmp_path, text())
    assert len(hit) == 1 and hit[0].level == "warning"
    msg = hit[0].message + (hit[0].fix or "")
    assert "email (email)" in msg and "tax_id (cpf)" in msg and "city" not in hit[0].message
    assert "email: hash" in hit[0].fix and "sensitive: false" in hit[0].fix
    assert EMAIL not in msg and CPF not in msg and "bo@example.org" not in msg
    assert hit[0].line == 7


def test_protect_covers_the_fields(tmp_path):
    protect = (
        "  - id: p\n    use: protect\n    input: src\n"
        "    with: { key: abc, fields: { email: hash, tax_id: hash } }\n"
    )
    assert not al014(tmp_path, text(protect, "p"))
    partial = "  - id: p\n    use: protect\n    input: src\n    with: { key: abc, fields: [email] }\n"
    (hit,) = al014(tmp_path, text(partial, "p"))
    assert "tax_id (cpf)" in hit.message and "email" not in hit.message


def test_sensitive_false_and_pii_check_silence_it(tmp_path):
    assert not al014(tmp_path, text(extra="    sensitive: false\n"))
    assert al014(tmp_path, text(extra="    sensitive: true\n"))
    check = "  - id: c\n    check: src\n    expect: { pii: true }\n"
    assert not al014(tmp_path, text(check, "c"))
    off = "  - id: c\n    check: src\n    expect: { pii: false }\n"
    assert al014(tmp_path, text(off, "c"))


def test_no_false_positive_without_personal_data(tmp_path):
    (tmp_path / "in.csv").write_text("id,city,amount\n1,Recife,10\n2,Natal,20\n")
    assert not [f for f in lint.check_text(text(), base_dir=str(tmp_path)).findings if f.code == "AL014"]


def test_sensitive_belongs_to_writers():
    with pytest.raises(SpecError, match="sensitive belongs to writer steps"):
        parse(
            {
                "name": "x",
                "steps": [{"id": "a", "use": "csv.read", "with": {"path": "a"}, "sensitive": False}],
            }
        )


def test_preview_lists_suspected_fields_and_suggests_protect(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "in.csv").write_text(CSV)
    spec = parse(yaml.safe_load(text()))
    with Store(str(tmp_path / "s.db")) as store:
        shown = preview(spec, store, "save")
    kinds = {(f["in"], f["field"], f["kind"]) for f in shown["pii"]}
    assert kinds == {("src", "email", "email"), ("src", "tax_id", "cpf")}
    assert shown["suggest"] == {
        "use": "protect",
        "with": {"fields": {"email": "hash", "tax_id": "hash"}, "key": "${AGENTL_DATA_KEY}"},
    }
    assert EMAIL not in json.dumps(shown["pii"]) and CPF not in json.dumps(shown["pii"])
    with Store(str(tmp_path / "s.db")) as store:
        src = preview(spec, store, "src")
    assert src["pii"] and all(f["in"] == "output" for f in src["pii"])


def test_cli_preview_without_personal_data(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "in.csv").write_text("id,city\n1,Recife\n")
    (tmp_path / "p.yaml").write_text(text())
    assert main(["preview", "p.yaml", "save", "--db", str(tmp_path / "s.db")]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["pii"] == [] and "suggest" not in shown
