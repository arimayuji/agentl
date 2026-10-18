"""`agentl repair`: a checked fix proposal for a failed run, never applied (#299)."""

import json
from typing import ClassVar

from agentl import repair
from agentl.cli import main
from agentl.llm import Result, register_provider
from agentl.mcp_server import Server
from agentl.runner import Runner
from agentl.spec import load
from agentl.store import Store

PIPELINE = """name: orders
llm:
  default: {provider: fixer, model: fix-1, price: {input: 1.0, output: 2.0}}
steps:
  - id: rows
    use: csv.read
    with: {path: orders.csv}
  - id: total
    fn: tests.test_repair:total
    input: rows
    with: {field: amount}
"""

GOOD_PATCH = """--- pipeline.yaml
+++ pipeline.yaml
@@ -9,4 +9,4 @@
   - id: total
     fn: tests.test_repair:total
     input: rows
-    with: {field: amount}
+    with: {field: total_amount}
"""

BAD_PATCH = """--- pipeline.yaml
+++ pipeline.yaml
@@ -9,2 +9,2 @@
   - id: total
-    fn: tests.test_repair:total
+    fnn: tests.test_repair:total
"""

SECRET = "sk-very-secret-value-123"


def total(input, field="amount"):
    return [{"sum": sum(float(r[field]) for r in input)}]


class Fixer:
    ANSWERS: ClassVar[list] = []
    SEEN: ClassVar[list] = []

    def __init__(self, spec):
        pass

    def structured(self, instruction, record, schema):
        self.SEEN.append(json.dumps(record))
        return Result(schema.model_validate(self.ANSWERS.pop(0)), 1000, 200)

    def text(self, instruction, content):
        return ""


register_provider("fixer", Fixer)


def _answer(patch):
    return {
        "diagnosis": "The CSV column is total_amount, not amount.",
        "confidence": 0.9,
        "patch": patch,
        "notes": ["rerun after the fix"],
    }


def _setup(tmp_path, monkeypatch, *answers):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CRM_API_TOKEN", SECRET)
    (tmp_path / "orders.csv").write_text(
        "email,total_amount,note\nann.lee@example.com,10.5," + SECRET + "\nbob@example.com,3,ok\n"
    )
    path = tmp_path / "pipeline.yaml"
    path.write_text(PIPELINE)
    with Store(tmp_path / "s.db") as store:
        run_id = Runner(load(path), store=store).run().run_id
    Fixer.ANSWERS[:] = list(answers)
    Fixer.SEEN.clear()
    return path, run_id


def test_valid_patch_is_shown_and_not_applied(tmp_path, monkeypatch, capsys):
    path, run_id = _setup(tmp_path, monkeypatch, _answer(GOOD_PATCH))
    code = main(["repair", run_id, "--pipeline", str(path), "--db", str(tmp_path / "s.db")])
    out = capsys.readouterr().out
    assert code == 0
    assert "total_amount, not amount" in out and "+    with: {field: total_amount}" in out
    assert "validation: ok" in out and "1 call(s)" in out
    assert path.read_text() == PIPELINE  # never applied
    with Store(tmp_path / "s.db") as store:
        assert next(e for e in store.events(run_id) if e["type"] == "repair.proposed")["data"]["valid"]


def test_invalid_patch_retries_then_gives_the_diagnosis_only(tmp_path, monkeypatch, capsys):
    path, run_id = _setup(tmp_path, monkeypatch, _answer(BAD_PATCH), _answer("not a diff"))
    code = main(["repair", run_id, "--pipeline", str(path), "--db", str(tmp_path / "s.db")])
    out = capsys.readouterr().out
    assert code == 1 and len(Fixer.SEEN) == 2
    assert "validation_errors" in Fixer.SEEN[1] and "previous_patch" in Fixer.SEEN[1]
    assert "total_amount, not amount" in out and "no valid patch after 2 attempt(s)" in out
    assert "fnn" not in out


def test_write_refuses_the_pipeline_and_writes_a_new_file(tmp_path, monkeypatch, capsys):
    path, run_id = _setup(tmp_path, monkeypatch, _answer(GOOD_PATCH))
    db = str(tmp_path / "s.db")
    assert main(["repair", run_id, "--pipeline", str(path), "--db", db, "--write", str(path)]) == 2
    assert path.read_text() == PIPELINE and not Fixer.SEEN  # refused before any llm call
    new = tmp_path / "fixed.yaml"
    assert main(["repair", run_id, "--pipeline", str(path), "--db", db, "--write", str(new)]) == 0
    assert "field: total_amount" in new.read_text() and path.read_text() == PIPELINE
    load(new)  # the patched file loads


def test_secrets_and_personal_data_stay_out_of_the_prompt(tmp_path, monkeypatch):
    path, run_id = _setup(tmp_path, monkeypatch, _answer(GOOD_PATCH))
    with Store(tmp_path / "s.db") as store:
        repair.propose(store, run_id, path.read_text(), str(path))
    sent = Fixer.SEEN[0]
    assert SECRET not in sent and "ann.lee@example.com" not in sent
    assert "<email>" in sent and "total_amount" in sent and "CRM_API_TOKEN" not in sent


def test_json_shape_and_budget(tmp_path, monkeypatch, capsys):
    path, run_id = _setup(tmp_path, monkeypatch, _answer(BAD_PATCH), _answer(GOOD_PATCH))
    assert main(["repair", run_id, "--pipeline", str(path), "--db", str(tmp_path / "s.db"), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] and result["attempts"] == 2 and not result["applied"]
    assert {"diagnosis", "confidence", "patch", "notes", "errors", "usage", "failed_step"} <= set(result)
    assert result["failed_step"] == "total"
    assert result["usage"] == {
        "llm_calls": 2,
        "input_tokens": 2000,
        "output_tokens": 400,
        "cost_usd": 0.0028,
    }


def test_mcp_tool_is_read_only(tmp_path, monkeypatch):
    path, run_id = _setup(tmp_path, monkeypatch, _answer(GOOD_PATCH))
    server = Server(db=str(tmp_path / "s.db"), root=str(tmp_path))
    meta, fn = server.tools["repair"]
    assert meta["annotations"]["readOnlyHint"] is True
    result = fn({"run_id": run_id, "pipeline": str(path)})
    assert result["valid"] and "patched" not in result and path.read_text() == PIPELINE


def test_apply_patch_tolerates_wrong_line_numbers():
    text = "a\nb\nc\n"
    assert repair.apply_patch(text, "@@ -7,2 +7,2 @@\n b\n-c\n+d\n") == "a\nb\nd\n"
