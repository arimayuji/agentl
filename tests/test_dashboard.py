from agentl import parse
from agentl.dashboard import PAGE, parse_graph, run_detail, step_stats
from agentl.runner import Runner
from agentl.store import Store


def rows(input=None):
    return [{"i": 1}, {"i": 2}]


def boom(input=None):
    raise RuntimeError("kaboom")


def test_run_detail_has_graph_and_step_stats(tmp_path):
    spec = parse(
        {
            "name": "d",
            "steps": [
                {"id": "a", "fn": "tests.test_dashboard:rows"},
                {"id": "b", "use": "select", "input": "a"},
                {"id": "c", "fn": "tests.test_dashboard:boom", "input": "b"},
            ],
        }
    )
    with Store(tmp_path / "s.db") as store:
        result = Runner(spec, store=store).run()
        d = run_detail(store, result.run_id)
    assert [n["id"] for n in d["graph"]["nodes"]] == ["a", "b", "c"]
    assert d["graph"]["nodes"][0]["kind"] == "fn" and d["graph"]["nodes"][1]["detail"] == "select"
    assert d["graph"]["edges"] == [["a", "b"], ["b", "c"]]
    assert d["steps"]["a"]["status"] == "succeeded" and d["steps"]["a"]["records"] == 2
    assert d["steps"]["c"]["status"] == "failed" and "kaboom" in d["steps"]["c"]["error"]
    assert d["steps"]["c"]["last"] is True and d["steps"]["a"]["last"] is False


def test_parse_graph_shapes():
    g = parse_graph(
        'flowchart LR\n  x{{"x<br/><small>agent</small>"}}\n  y["y<br/><small>csv.read</small>"]\n  y --> x'
    )
    assert g == {
        "nodes": [
            {"id": "x", "detail": "agent", "kind": "agent"},
            {"id": "y", "detail": "csv.read", "kind": "use"},
        ],
        "edges": [["y", "x"]],
    }
    assert step_stats([]) == {}


def test_page_is_self_contained():
    assert "<script src=" not in PAGE and "https://" not in PAGE  # no CDN, works offline


# labels, grouping and static export (#295) ---------------------------------------------------

import json  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

from agentl import labels as run_labels  # noqa: E402
from agentl.cli import main  # noqa: E402
from agentl.dashboard import export, groups, redact, summary  # noqa: E402
from agentl.spec import SpecError  # noqa: E402


def _spec(name, labels=None, fail=False):
    steps = [
        {"id": "a", "use": "csv.read", "with": {"path": "in.csv"}},
        {"id": "b", "fn": "tests.test_dashboard:boom" if fail else "tests.test_dashboard:rows", "input": "a"},
        {"id": "c", "use": "csv.write", "input": "b", "with": {"path": "out.csv"}},
    ]
    return parse({"name": name, "labels": labels or {}, "steps": steps})


def test_labels_are_validated():
    assert parse({"name": "x", "labels": {"team": "sales"}, "steps": [{"id": "a", "fn": "m:f"}]}).labels
    for bad in ({"team": 3}, {"bad key": "x"}, {"team": "a\nb"}, ["team"]):
        with pytest.raises((SpecError, ValueError)):
            parse({"name": "x", "labels": bad, "steps": [{"id": "a", "fn": "m:f"}]})


def test_inferred_labels_and_spec_override():
    spec = _spec("p", {"team": "sales"})
    assert run_labels.infer(spec) == {"source": "csv", "sink": "csv"}
    assert run_labels.of(_spec("p", {"sink": "lake"}))["sink"] == "lake"
    assert run_labels.matches({"source": "csv,http"}, {"source": "http"})
    assert not run_labels.matches({"team": "a"}, {"team": "b"})
    assert run_labels.parse_filters(["team=a", "domain = b"]) == {"team": "a", "domain": "b"}
    with pytest.raises(ValueError):
        run_labels.parse_filters(["team"])


def _seed(tmp_path, monkeypatch, store):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "in.csv").write_text("i\n1\n2\n")
    Runner(_spec("orders", {"team": "sales", "domain": "orders"}), store=store).run()
    Runner(_spec("leads", {"team": "sales"}, fail=True), store=store).run()
    Runner(_spec("tickets", {"team": "support"}), store=store).run()


def test_labels_stored_filtered_and_grouped(tmp_path, monkeypatch):
    with Store(tmp_path / "s.db") as store:
        _seed(tmp_path, monkeypatch, store)
        assert {r["pipeline"] for r in store.runs(labels={"team": "sales"})} == {"orders", "leads"}
        assert store.runs(labels={"source": "csv"})[0]["labels"]["sink"] == "csv"
        rows = summary(store, 0)
    by_team = {g["value"]: g for g in groups(rows, "team")}
    assert by_team["sales"]["pipeline_count"] == 2 and by_team["sales"]["failing"] == 1
    assert by_team["sales"]["runs"] == 2 and by_team["support"]["failing"] == 0
    assert by_team["support"]["records"] == 2  # rows written by csv.write
    assert [p["pipeline"] for p in by_team["sales"]["pipelines"]] == ["leads", "orders"]
    by_domain = {g["value"]: g for g in groups(rows, "domain")}
    assert by_domain["(none)"]["pipeline_count"] == 2
    assert "cost_7d" in by_team["sales"] and "cost_30d" in by_team["sales"]


def test_runs_cli_label_filter(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    with Store(db) as store:
        _seed(tmp_path, monkeypatch, store)
    assert main(["runs", "--db", str(db), "--label", "team=support"]) == 0
    out = capsys.readouterr().out
    assert "tickets" in out and "orders" not in out and "team=support" in out


def test_redact_masks_secrets():
    data = {
        "api_key": "abc",
        "url": "postgresql://u:pw@h/db",
        "summary": "rows",
        "n": 1,
        "e": "Authorization: Bearer abcdefghijkl",
        "k": "key sk-ant-0123456789abcdef",
    }
    out = redact(data)
    assert out["api_key"] == "***" and "pw" not in out["url"] and "summary" not in out
    assert "abcdefghijkl" not in out["e"] and "0123456789abcdef" not in out["k"] and out["n"] == 1


def test_export_writes_static_site_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_PW", "hunter2-very-secret")
    with Store(tmp_path / "s.db") as store:
        _seed(tmp_path, monkeypatch, store)
        store.event(
            store.runs()[0]["id"],
            "step.failed",
            "b",
            {"error": "connect postgresql://bob:hunter2-very-secret@db/x", "token": "t0ken"},
        )
        done = export(store, tmp_path / "site", time.time() - 86400, {"team": "sales"})
    site = tmp_path / "site"
    assert done["runs"] == 2
    page = (site / "index.html").read_text()
    assert "window.AGENTL_STATIC=" in page
    assert "<script src" not in page and "<link" not in page  # no CDN
    runs = json.loads((site / "runs.json").read_text())
    assert {r["pipeline"] for r in runs} == {"orders", "leads"}
    assert set(json.loads((site / "groups.json").read_text())) >= {"team", "source"}
    assert len(list((site / "runs").glob("*.json"))) == 2
    everything = "".join(p.read_text() for p in site.rglob("*") if p.is_file())
    assert "hunter2-very-secret" not in everything and "t0ken" not in everything
    assert '"i": 1' not in everything  # no record payloads


def test_dashboard_export_cli(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    with Store(db) as store:
        _seed(tmp_path, monkeypatch, store)
    assert main(["dashboard", "export", str(tmp_path / "out"), "--db", str(db), "--since", "1d"]) == 0
    assert (tmp_path / "out" / "index.html").exists() and "3 runs" in capsys.readouterr().out
    assert main(["dashboard", "export", "--db", str(db)]) == 2


def test_page_has_grouping_controls():
    assert 'id="by"' in PAGE and "AGENTL_STATIC" in PAGE and "/api/summary" in PAGE
