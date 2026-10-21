"""Raw zone (#208): `persist: raw` keeps originals, `replay --from-raw` reprocesses them."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agentl import parse, raw
from agentl.cli import main
from agentl.raw import ReplayRefused
from agentl.registry import connector
from agentl.runner import Runner
from agentl.store import Store

CALLS: dict[str, int] = {}
PAYLOAD: list[dict] = []


@connector("test_raw.fetch")
def fetch(input=None):
    CALLS["fetch"] = CALLS.get("fetch", 0) + 1
    return [dict(r) for r in PAYLOAD]


@connector("test_raw.scale")
def scale(input, factor=2):
    CALLS["scale"] = CALLS.get("scale", 0) + 1
    return [{"n": r["n"] * factor} for r in input]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    CALLS.clear()
    PAYLOAD[:] = [{"n": 1}, {"n": 2}]
    monkeypatch.delenv("AGENTL_RAW_DIR", raising=False)


def _spec(factor: int = 2, keep_days: float | None = None, raw_on: bool = True, extra=None):
    steps = [
        {"id": "download", "use": "test_raw.fetch", **({"persist": "raw"} if raw_on else {})},
        {"id": "rows", "use": "test_raw.scale", "input": "download", "with": {"factor": factor}},
        *(extra or []),
    ]
    d: dict = {"name": "rawzone", "steps": steps}
    if keep_days is not None:
        d["raw"] = {"keep_days": keep_days}
    return parse(d)


def test_originals_are_content_addressed_and_never_overwritten(tmp_path):
    with Store(tmp_path / "a.db") as store:
        a = Runner(_spec(), store=store).run()
        b = Runner(_spec(), store=store).run()
        base = tmp_path / "raw"
        ms = raw.manifests(base)
        assert [m["run_id"] for m in ms] == [a.run_id, b.run_id]
        assert ms[0]["sha256"] == ms[1]["sha256"]  # same payload, one object
        objects = list((base / "objects").glob("*/*.json"))
        assert len(objects) == 1 and objects[0].stem == ms[0]["sha256"]
        assert objects[0].stat().st_mode & 0o222 == 0  # read-only
        ev = next(e for e in store.events(a.run_id) if e["type"] == "raw.stored")
        assert ev["step"] == "download" and ev["data"]["records"] == 2


def test_replay_reprocesses_with_current_code_without_fetching(tmp_path):
    with Store(tmp_path / "a.db") as store:
        first = Runner(_spec(), store=store).run()
        PAYLOAD[:] = [{"n": 100}]  # the source moved on; the replay must not see it
        result = Runner(_spec(factor=10), store=store, raw_from=first.run_id).run()
        assert result.status == "succeeded"
        assert CALLS["fetch"] == 1
        assert result.outputs["rows"] == [{"n": 10}, {"n": 20}]
        events = store.events(result.run_id)
        started = next(e for e in events if e["type"] == "run.started")
        assert started["data"]["replayed_from_raw"] == first.run_id
        assert not [e for e in events if e["type"] == "raw.stored"]  # replay keeps no new copy


def test_replay_skips_upstream_of_the_raw_step(tmp_path):
    extra = [
        {"id": "keep", "use": "test_raw.scale", "input": "rows", "with": {"factor": 1}, "persist": "raw"}
    ]
    with Store(tmp_path / "a.db") as store:
        spec = parse(
            {
                "name": "rawzone",
                "steps": [
                    {"id": "download", "use": "test_raw.fetch"},
                    {"id": "rows", "use": "test_raw.scale", "input": "download"},
                    *extra,
                    {"id": "out", "use": "test_raw.scale", "input": "keep", "with": {"factor": 3}},
                ],
            }
        )
        first = Runner(spec, store=store).run()
        result = Runner(spec, store=store, raw_from=first.run_id).run()
        assert CALLS == {"fetch": 1, "scale": 4}  # rows+keep skipped, only out ran again
        assert result.outputs["out"] == [{"n": 6}, {"n": 12}]


def test_replay_refusals(tmp_path):
    with Store(tmp_path / "a.db") as store:
        with pytest.raises(ReplayRefused, match="no step with `persist: raw`"):
            Runner(_spec(raw_on=False), store=store, raw_from="x").run()
        with pytest.raises(ReplayRefused, match="no originals"):
            Runner(_spec(), store=store, raw_from="nope").run()
        first = Runner(_spec(), store=store).run()
        spec = parse(
            {
                "name": "rawzone",
                "steps": [
                    {"id": "fetch", "use": "test_raw.fetch"},
                    {"id": "download", "use": "test_raw.scale", "input": "fetch", "persist": "raw"},
                    {"id": "other", "use": "test_raw.scale", "input": "fetch"},
                ],
            }
        )
        with pytest.raises(ReplayRefused, match="runs before the raw zone"):
            Runner(spec, store=store, raw_from=first.run_id).run()
        obj = next((tmp_path / "raw" / "objects").glob("*/*.json"))
        obj.chmod(0o600)
        obj.write_text('{"records": []}')
        with pytest.raises(ReplayRefused, match="changed on disk"):
            Runner(_spec(), store=store, raw_from=first.run_id).run()


def test_retention_drops_old_manifests_and_unreferenced_objects(tmp_path):
    with Store(tmp_path / "a.db") as store:
        old = Runner(_spec(), store=store).run()
        base = tmp_path / "raw"
        f = Path(raw.manifests(base)[0]["_file"])
        m = json.loads(f.read_text())
        m["stored_at"] = time.time() - 10 * 86400
        f.write_text(json.dumps(m))
        PAYLOAD[:] = [{"n": 5}]
        Runner(_spec(keep_days=7), store=store).run()
        ms = raw.manifests(base)
        assert old.run_id not in {m["run_id"] for m in ms} and len(ms) == 1
        assert [p.stem for p in (base / "objects").glob("*/*.json")] == [ms[0]["sha256"]]


def test_cli_raw_ls_replay_and_prune(tmp_path, capsys):
    yaml = tmp_path / "p.yaml"
    yaml.write_text(
        "name: rawzone\nsteps:\n  - id: download\n    use: test_raw.fetch\n    persist: raw\n"
        "  - id: rows\n    use: test_raw.scale\n    input: download\n"
    )
    db = str(tmp_path / "a.db")
    assert main(["run", str(yaml), "--db", db, "--no-check"]) == 0
    capsys.readouterr()
    assert main(["raw", "ls", "--db", db, "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1 and rows[0]["step"] == "download"
    run_id = rows[0]["run_id"]
    assert main(["raw", "ls", str(yaml), "--db", db]) == 0
    assert run_id in capsys.readouterr().out
    assert main(["replay", str(yaml), "--from-raw", run_id, "--db", db]) == 0
    assert CALLS["fetch"] == 1
    assert main(["replay", str(yaml), "--from-raw", "missing", "--db", db]) == 2
    assert main(["raw", "prune", "--db", db]) == 2
    for m in raw.manifests(tmp_path / "raw"):  # age the originals past the retention
        f = Path(m.pop("_file"))
        f.write_text(json.dumps(m | {"stored_at": time.time() - 2 * 86400}))
    assert main(["raw", "prune", "--db", db, "--keep-days", "1"]) == 0
    assert "dropped 1 manifest(s) and 1 original(s)" in capsys.readouterr().out
