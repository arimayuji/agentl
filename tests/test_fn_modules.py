import sys

import agentl


def make(tmp_path, name, value, helper=False):
    d = tmp_path / name
    d.mkdir()
    if helper:
        (d / "helpers.py").write_text(f"VALUE = {value!r}\n")
        body = "from helpers import VALUE\n\ndef tag(input=None):\n    return [{'v': VALUE}]\n"
    else:
        body = f"def tag(input=None):\n    return [{{'v': {value!r}}}]\n"
    (d / "steps.py").write_text(body)
    (d / "p.yaml").write_text("name: p\nsteps:\n  - {id: t, fn: 'steps:tag'}\n")
    return d / "p.yaml"


def test_same_module_name_in_two_pipelines(tmp_path):
    before = list(sys.path)
    a = make(tmp_path, "a", "from-a")
    b = make(tmp_path, "b", "from-b")
    db = tmp_path / "s.db"
    assert agentl.run(a, db=db).outputs["t"] == [{"v": "from-a"}]
    assert agentl.run(b, db=db).outputs["t"] == [{"v": "from-b"}]
    assert agentl.run(a, db=db).outputs["t"] == [{"v": "from-a"}]
    assert "steps" not in sys.modules
    assert sys.path == before


def test_sibling_imports_inside_a_local_module(tmp_path):
    p = make(tmp_path, "c", "helper-value", helper=True)
    assert agentl.run(p, db=tmp_path / "s.db").outputs["t"] == [{"v": "helper-value"}]


def test_installed_modules_and_working_dir(tmp_path, monkeypatch):
    from agentl.registry import resolve_fn

    assert resolve_fn("json:dumps") is __import__("json").dumps
    (tmp_path / "cwd_steps.py").write_text("def f(input=None):\n    return [{'ok': 1}]\n")
    monkeypatch.chdir(tmp_path)
    assert resolve_fn("cwd_steps:f")() == [{"ok": 1}]
