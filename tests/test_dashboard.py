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
