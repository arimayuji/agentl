"""Incremental loading with a cursor kept in the run store (#66)."""

import http.server
import json
import threading
from urllib.parse import parse_qs, urlparse

import pytest

from agentl import parse
from agentl.cli import main
from agentl.runner import Runner
from agentl.spec import SpecError
from agentl.store import Store

ROWS = [{"id": i, "updated_at": f"2026-10-0{i}"} for i in range(1, 4)]


@pytest.fixture
def api():
    """GET /orders?since=X returns the rows updated after X; records what was asked."""
    seen: list[str] = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            since = parse_qs(urlparse(self.path).query).get("since", [""])[0]
            seen.append(since)
            data = json.dumps([r for r in ROWS if r["updated_at"] > since]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()
    srv.server_close()


FAIL = {"on": False}


def sink(input=None):
    if FAIL["on"]:
        raise RuntimeError("warehouse down")
    return input


def spec(url):
    return parse(
        {
            "name": "orders",
            "steps": [
                {
                    "id": "orders",
                    "use": "http.get",
                    "with": {"url": f"{url}/orders", "params": {"since": "{cursor}"}},
                    "cursor": {"field": "updated_at", "initial": "2026-01-01"},
                },
                {"id": "load", "fn": "tests.test_cursor:sink", "input": "orders"},
            ],
        }
    )


def test_second_run_only_fetches_new_records_and_failures_do_not_advance(api, tmp_path):
    url, seen = api
    with Store(tmp_path / "s.db") as store:
        r1 = Runner(spec(url), store=store).run()
        assert r1.status == "succeeded" and len(r1.outputs["orders"]) == 3
        assert store.state_get("orders", "orders") == "2026-10-03"

        ROWS.append({"id": 4, "updated_at": "2026-10-04"})
        FAIL["on"] = True
        r2 = Runner(spec(url), store=store).run()
        assert r2.status == "failed" and store.state_get("orders", "orders") == "2026-10-03"

        FAIL["on"] = False
        r3 = Runner(spec(url), store=store).run()
        assert [o["id"] for o in r3.outputs["orders"]] == [4]
        assert store.state_get("orders", "orders") == "2026-10-04"
        types = [e["type"] for e in store.events(r3.run_id)]

        r4 = Runner(spec(url), store=store).run()  # nothing new: the cursor stays
        assert r4.outputs["orders"] == [] and store.state_get("orders", "orders") == "2026-10-04"
    assert seen == ["2026-01-01", "2026-10-03", "2026-10-03", "2026-10-04"]
    assert "cursor.advanced" in types
    ROWS.pop()


def numbers(input=None, after=0):
    return [{"n": n} for n in range(after + 1, 6)]


def test_whole_placeholder_keeps_the_type_and_cli_shows_and_resets(tmp_path, capsys):
    p = parse(
        {
            "name": "nums",
            "steps": [
                {
                    "id": "src",
                    "fn": "tests.test_cursor:numbers",
                    "with": {"after": "{cursor}"},
                    "cursor": {"field": "n", "initial": 0},
                }
            ],
        }
    )
    db = tmp_path / "s.db"
    with Store(db) as store:
        assert len(Runner(p, store=store).run().outputs["src"]) == 5
        assert Runner(p, store=store).run().outputs["src"] == []
    assert main(["state", "show", "--db", str(db)]) == 0
    assert "nums  src  5" in capsys.readouterr().out
    assert main(["state", "reset", "nums", "--db", str(db)]) == 0
    with Store(db) as store:
        assert store.state_get("nums", "src") is None
        assert len(Runner(p, store=store).run().outputs["src"]) == 5
    assert main(["state", "reset", "--db", str(db)]) == 2


def test_cursor_only_on_source_steps():
    with pytest.raises(SpecError, match="cursor goes on a use or fn step"):
        parse(
            {
                "name": "x",
                "steps": [
                    {"id": "a", "fn": "tests.test_cursor:numbers"},
                    {"id": "b", "check": "a", "expect": {"rows": ">0"}, "cursor": {"field": "n"}},
                ],
            }
        )
