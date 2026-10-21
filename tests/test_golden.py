"""Golden sets: `agentl eval` scores an agent step on records with known answers (#154)."""

import json

import pytest

from agentl import llm
from agentl.cli import main
from agentl.golden import matches, parse_override
from agentl.spec import parse


class Keyword:
    """Fake model: 'travel' when the text mentions a flight; the 'smart' model also knows taxis."""

    def __init__(self, spec):
        self.spec = spec

    def structured(self, instruction, record, schema):
        text = record["text"].lower()
        travel = "flight" in text or (self.spec.model == "smart" and "taxi" in text)
        value = {"category": "travel" if travel else "food", "amount": record["price"] + 0.004}
        return llm.Result(schema.model_validate(value), input_tokens=10, output_tokens=5)


PIPELINE = """
name: expenses
llm:
  default: {{provider: keyword, model: basic, price: {{input: 1, output: 2}}}}
steps:
  - id: load
    use: json.read
    with: {{path: data.json}}
  - id: classify
    input: load
    agent: Classify the expense.
    output: {{category: str, amount: float}}
    golden: {golden}
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    llm.register_provider("keyword", Keyword)
    rows = [
        {"text": "Flight to Lisbon", "price": 300, "category": "travel", "amount": 300},
        {"text": "Lunch", "price": 12.5, "category": "food", "amount": 12.5},
        {"text": "Taxi to airport", "price": 40, "category": "travel", "amount": 40},
    ]
    (tmp_path / "golden").mkdir()
    (tmp_path / "golden" / "classify.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    def write(golden):
        (tmp_path / "pipeline.yaml").write_text(PIPELINE.format(golden=golden), encoding="utf-8")
        return str(tmp_path / "pipeline.yaml")

    return write


def test_eval_reports_accuracy_cost_and_differences(project, capsys, tmp_path):
    path = project("{file: golden/classify.jsonl, tolerance: 0.01}")
    assert main(["eval", path, "classify", "--json", "--db", str(tmp_path / "x.db")]) == 0
    [report] = json.loads(capsys.readouterr().out)
    assert report["records"] == 3
    assert report["fields"]["category"] == pytest.approx(2 / 3)
    assert report["fields"]["amount"] == 1.0  # within the tolerance
    assert report["accuracy"] == pytest.approx(2 / 3)
    assert report["cost_usd"] == pytest.approx(3 * (10 + 5 * 2) / 1e6)
    assert report["fields"]["category"] == pytest.approx(2 / 3)
    assert report["cost_per_right"] == pytest.approx(3 * (10 + 5 * 2) / 1e6 / 2, abs=1e-6)
    assert report["diffs"] == [{"line": 3, "field": "category", "expected": "travel", "got": "food"}]


def test_threshold_fails_and_compare_runs_side_by_side(project, capsys):
    path = project("{file: golden/classify.jsonl, min_accuracy: 0.9, tolerance: 0.01}")
    assert main(["eval", path, "--compare", "model=smart"]) == 1
    out = capsys.readouterr().out
    assert "classify [default]: 66.7% of 3 records right FAIL (min 90%)" in out
    assert "classify [model=smart]: 100.0% of 3 records right ok" in out
    assert "$0.000030 per right answer" in out  # 2 of 3 right, 0.00002 USD a call
    assert "$0.000020 per right answer" in out  # the smart model gets all 3 right


def test_without_tolerance_numbers_must_match(project, capsys):
    assert main(["eval", project("golden/classify.jsonl"), "--json"]) == 0
    [report] = json.loads(capsys.readouterr().out)
    assert report["fields"]["amount"] == 0.0


def test_golden_only_on_agent_steps_and_helpers():
    with pytest.raises(ValueError, match="only agent steps take a golden set"):
        parse({"name": "p", "steps": [{"id": "a", "use": "json.read", "golden": "g.jsonl"}]})
    assert matches(1, 1.0) and not matches(True, 1) and matches(1.0, 1.05, 0.1)
    assert parse_override("model=x") == {"model": "x"}
    with pytest.raises(ValueError):
        parse_override("temperature=1")


def no_rules(records):
    """Hybrid rules that never answer: every record goes to the model."""
    return [None for _ in records]


def test_eval_scores_hybrid_steps(project, capsys):
    """The `_path` tag a hybrid step adds to its answers must not hide them from the scorer."""
    path = project("golden/classify.jsonl")
    with open(path, encoding="utf-8") as f:
        text = f.read().replace(
            "    golden:", "    rules:\n      - fn: tests.test_golden:no_rules\n    golden:"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    assert main(["eval", path, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    report = report[0]
    assert report["fields"]["category"] == pytest.approx(2 / 3)


def git(cwd, *args):
    import subprocess

    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True
    )


class Prompted:
    """Fake model that only knows taxis are travel when the prompt says so."""

    def __init__(self, spec):
        self.spec = spec

    def structured(self, instruction, record, schema):
        text = record["text"].lower()
        travel = "flight" in text or ("taxi" in instruction.lower() and "taxi" in text)
        value = {"category": "travel" if travel else "food", "amount": record["price"]}
        return llm.Result(schema.model_validate(value), input_tokens=10, output_tokens=5)


def test_compare_prompt_at_a_git_ref(project, capsys, tmp_path):
    llm.register_provider("keyword", Prompted)
    path = project("golden/classify.jsonl")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "pipeline.yaml")
    git(tmp_path, "commit", "-qm", "old prompt")
    text = (tmp_path / "pipeline.yaml").read_text(encoding="utf-8")
    (tmp_path / "pipeline.yaml").write_text(
        text.replace("Classify the expense.", "Classify the expense text by price; a taxi is travel."),
        encoding="utf-8",
    )
    assert main(["eval", path, "--compare", "prompt@HEAD"]) == 0
    out = capsys.readouterr().out
    assert "classify [default]: 100.0% of 3 records right" in out
    assert "classify [prompt@HEAD]: 66.7% of 3 records right" in out
    assert "category: 66.7%  (vs default: -33.3%)" in out
    assert main(["eval", path, "--compare", "prompt@--output=x"]) != 0
    assert "needs a git ref" in capsys.readouterr().err
    assert main(["eval", path, "--compare", "prompt@nope"]) != 0
    assert "prompt@nope" in capsys.readouterr().err


def test_eval_add_from_run_appends_the_answers(project, capsys, tmp_path):
    from agentl import run

    path = project("golden/classify.jsonl")
    (tmp_path / "data.json").write_text(
        json.dumps([{"text": "Taxi home", "price": 20}, {"text": "Flight to Lisbon", "price": 300}]),
        encoding="utf-8",
    )
    db = str(tmp_path / "runs.db")
    result = run(path, db=db)
    assert result.status == "succeeded"
    argv = ["eval", "add", path, "classify", "--from-run", result.run_id, "--db", db]
    assert main(argv) == 0
    assert "added 1 golden record(s)" in capsys.readouterr().out  # the flight is already in the file
    lines = (tmp_path / "golden" / "classify.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1]) == {"text": "Taxi home", "price": 20, "category": "food", "amount": 20.004}
    assert main(argv) == 0
    assert "added 0 golden record(s)" in capsys.readouterr().out
    assert main(["eval", "add", path, "classify"]) == 2


def test_lint_flags_golden_fields_the_prompt_never_names(project, tmp_path):
    from agentl import lint

    path = project("golden/classify.jsonl")
    codes = [f for f in lint.check_file(path).findings if f.code == "AL014"]
    assert len(codes) == 1 and "text, price" in codes[0].message
    text = (tmp_path / "pipeline.yaml").read_text(encoding="utf-8")
    (tmp_path / "pipeline.yaml").write_text(
        text.replace("Classify the expense.", "Classify the expense from its text and price."),
        encoding="utf-8",
    )
    assert not [f for f in lint.check_file(path).findings if f.code == "AL014"]
