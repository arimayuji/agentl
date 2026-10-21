"""`agentl new --template` (#148): the packaged templates are the tested recipes, and each lints
clean, with no LLM call."""

import importlib.util
from pathlib import Path

import pytest

from agentl import templates
from agentl.cli import main
from agentl.lint import check_text

ROOT = Path(__file__).parent.parent


def _sync():
    spec = importlib.util.spec_from_file_location("sync_templates", ROOT / "tools" / "sync_templates.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("group", ["recipes", "patterns"])
def test_templates_match_the_recipes(group):
    want = _sync().expected(group)
    assert templates.names(group) == sorted(want), "run: python tools/sync_templates.py"
    for name, text in want.items():
        assert templates.group(name) == group
        assert templates.text(name) == text, f"template {name} is stale: run python tools/sync_templates.py"


def test_template_names_are_unique_across_groups():
    assert not set(templates.names("recipes")) & set(templates.names("patterns"))
    assert len(templates.names()) == len(templates.names("recipes")) + len(templates.names("patterns"))


@pytest.mark.parametrize("name", templates.names())
def test_every_template_lints_without_errors(name):
    report = check_text(templates.text(name), file=f"{name}.yaml")
    assert not report.errors, report.render("text")
    assert templates.summary(name)


def test_new_list_and_template(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["new", "--list"]) == 0
    listed = capsys.readouterr().out
    line = next(x for x in listed.splitlines() if x.startswith("incremental-api-load "))
    assert "Load only what changed" in line
    lines = listed.splitlines()
    assert lines[0] == "recipes:" and "patterns:" in lines
    scd2 = next(i for i, x in enumerate(lines) if x.startswith("scd2-history "))
    assert scd2 > lines.index("patterns:")
    assert main(["new", "-t", "scd2-history"]) == 0
    assert "mode: scd2" in capsys.readouterr().out
    assert main(["new", "-t", "incremental-api-load", "-o", "pipeline.yaml"]) == 0
    assert (tmp_path / "pipeline.yaml").read_text() == templates.text("incremental-api-load")
    capsys.readouterr()
    assert main(["new", "-t", "incremental-api-load", "-o", "pipeline.yaml"]) == 2  # never overwrites
    assert "File exists" in capsys.readouterr().err
    assert main(["new", "-t", "classify-ticket"]) == 2
    assert "Did you mean 'classify-tickets'?" in capsys.readouterr().err
    assert main(["new"]) == 2


def test_templates_ship_in_the_wheel():
    text = (ROOT / "pyproject.toml").read_text()
    assert 'packages = ["agentl"]' in text  # hatch packs every file under agentl/, the YAML included
    assert list((ROOT / "agentl" / "templates").glob("*.yaml"))
