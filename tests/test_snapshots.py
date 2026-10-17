from agentl import snapshots
from agentl.cli import main


def test_diff_by_key_ignores_order_and_shows_fields():
    old = [{"id": 1, "v": "a", "n": 1}, {"id": 2, "v": "b"}, {"id": 3, "v": "c"}]
    new = [{"id": 3, "v": "c"}, {"id": 1, "v": "A", "n": 1}, {"id": 4, "v": "d"}]
    d = snapshots.diff_records(old, new, ["id"])
    assert d.changed == [("[1]", {"v": ("a", "A")})]
    assert d.added == [{"id": 4, "v": "d"}]
    assert d.removed == [{"id": 2, "v": "b"}]
    assert 'v: "a" -> "A"' in d.render("s")
    assert snapshots.diff_records(old, list(reversed(old)), ["id"]).empty
    assert snapshots.diff_records(old, list(reversed(old))).empty


def test_redaction():
    h = snapshots.redact_headers({"Authorization": "Bearer x", "X-Github-Token": "t", "Accept": "a"})
    assert h == {"Authorization": "***", "X-Github-Token": "***", "Accept": "a"}
    u = snapshots.redact_url("https://u:pw@h.test/p?api_key=s&page=2&access_token=z")
    assert u == "https://u:***@h.test/p?api_key=***&page=2&access_token=***"
    assert "pw" not in u and "s&" not in u


def test_cli_snapshot_flow(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "in.csv"
    data.write_text("id,name\n1,a\n2,b\n", encoding="utf-8")
    p = tmp_path / "p.yaml"
    p.write_text(
        "name: demo\nsteps:\n  - id: src\n    use: csv.read\n    with: { path: in.csv }\n", encoding="utf-8"
    )
    args = ["snapshot", str(p), "--key", "id", "--db", str(tmp_path / "db.sqlite")]
    assert main(args) == 1
    assert "no snapshot" in capsys.readouterr().out
    assert main([*args, "-u", "--yes"]) == 0
    assert (tmp_path / "snapshots" / "demo" / "src.json").exists()
    capsys.readouterr()
    assert main(args) == 0
    assert "src: ok (2 records)" in capsys.readouterr().out
    data.write_text("id,name\n2,b\n1,z\n", encoding="utf-8")
    assert main([*args, "--no-cache"]) == 1
    assert 'name: "a" -> "z"' in capsys.readouterr().out
    assert main([*args, "--no-cache", "-u"]) == 1  # not a terminal, no --yes
