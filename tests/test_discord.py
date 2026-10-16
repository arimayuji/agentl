"""discord.send against a local fake webhook (#145)."""

import http.server
import json
import threading
from typing import ClassVar

import pytest

from agentl.connectors import chat
from agentl.lint import check_text
from agentl.registry import get


class Hook(http.server.BaseHTTPRequestHandler):
    posts: ClassVar[list] = []
    fail: ClassVar[list] = []  # status codes to answer before succeeding

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if Hook.fail:
            code = Hook.fail.pop(0)
            data = json.dumps({"retry_after": 0.01} if code == 429 else {"message": "boom"}).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        Hook.posts.append((self.path, body))
        data = json.dumps({"id": str(len(Hook.posts)), "channel_id": "c1"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def hook(monkeypatch):
    monkeypatch.setenv("AGENTL_ALLOW_HOSTS", "127.0.0.1")
    Hook.posts, Hook.fail = [], []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/api/webhooks/1/abc"
    srv.shutdown()


ORDERS = [{"id": 1, "customer": "Ana", "total": 120.5}, {"id": 2, "customer": "Bruno", "total": None}]


def test_one_message_per_record_from_a_template(hook):
    out = get("discord.send")(ORDERS, webhook=hook, content="Order {id} from {customer}: {total}")
    assert [b["content"] for _, b in Hook.posts] == ["Order 1 from Ana: 120.5", "Order 2 from Bruno: "]
    assert all(p.endswith("?wait=true") for p, _ in Hook.posts)
    assert out == [
        {"message": 1, "id": "1", "channel_id": "c1", "status": "sent"},
        {"message": 2, "id": "2", "channel_id": "c1", "status": "sent"},
    ]


def test_mentions_in_records_never_ping(hook):
    get("discord.send")([{"t": "@everyone free money"}], webhook=hook, content="{t}")
    assert Hook.posts[0][1]["allowed_mentions"] == {"parse": []}


def test_template_fills_fields_only():
    rec = {"a": "x", "n": 3, "l": [1, 2]}
    assert (
        chat.fill("{a} {n} {l} {missing} {a.__class__} {n:>5}", rec)
        == "x 3 [1, 2] {missing} {a.__class__} {n:>5}"
    )


def test_batch_packs_records_under_the_limit(hook):
    rows = [{"line": "x" * 900} for _ in range(5)]
    get("discord.send")(rows, webhook=hook, content="{line}", mode="batch")
    sizes = [len(b["content"]) for _, b in Hook.posts]
    assert len(sizes) == 3 and max(sizes) <= chat.DISCORD_LIMIT


def test_default_view_and_embed(hook):
    get("discord.send")(
        ORDERS[:1],
        webhook=hook,
        embed={"title": "Order {id}", "description": "{customer}", "color": "#0070c9", "footer": "agentl"},
        username="agentl",
    )
    body = Hook.posts[0][1]
    assert "content" not in body and body["username"] == "agentl"
    assert body["embeds"] == [
        {"title": "Order 1", "description": "Ana", "color": 0x0070C9, "footer": {"text": "agentl"}}
    ]
    Hook.posts.clear()
    get("discord.send")(ORDERS[:1], webhook=hook)
    assert Hook.posts[0][1]["content"] == "**id**: 1\n**customer**: Ana\n**total**: 120.5"


def test_long_message_is_cut(hook):
    get("discord.send")([{"t": "y" * 5000}], webhook=hook, content="{t}")
    assert len(Hook.posts[0][1]["content"]) == chat.DISCORD_LIMIT


def test_rate_limit_and_server_errors_are_retried(hook, monkeypatch):
    monkeypatch.setattr(chat.time, "sleep", lambda s: None)
    Hook.fail = [429, 502]
    out = get("discord.send")(ORDERS[:1], webhook=hook, content="{id}")
    assert out[0]["status"] == "sent" and len(Hook.posts) == 1


def test_client_error_fails_with_discords_message(hook):
    Hook.fail = [404]
    with pytest.raises(RuntimeError, match="HTTP 404"):
        get("discord.send")(ORDERS[:1], webhook=hook, content="{id}")


def test_max_messages_stops_a_flood_before_posting(hook):
    with pytest.raises(ValueError, match="max_messages"):
        get("discord.send")([{"i": i} for i in range(51)], webhook=hook, content="{i}")
    assert Hook.posts == []


def test_bad_arguments():
    send = get("discord.send")
    with pytest.raises(ValueError, match="webhook"):
        send(ORDERS, content="x")
    with pytest.raises(ValueError, match="http"):
        send(ORDERS, webhook="file:///etc/passwd")
    with pytest.raises(ValueError, match="mode"):
        send(ORDERS, webhook="https://discord.com/api/webhooks/1/a", mode="all")
    with pytest.raises(ValueError, match="embed"):
        send(ORDERS, webhook="https://discord.com/api/webhooks/1/a", mode="batch", embed={"title": "x"})
    assert send([], webhook="https://discord.com/api/webhooks/1/a") == []


def test_private_address_is_refused_without_allow(monkeypatch):
    monkeypatch.delenv("AGENTL_ALLOW_HOSTS", raising=False)
    monkeypatch.delenv("AGENTL_ALLOW_PRIVATE_NETWORK", raising=False)
    with pytest.raises(Exception, match="AGENTL_ALLOW_HOSTS"):
        get("discord.send")(ORDERS[:1], webhook="http://127.0.0.1:9/api/webhooks/1/a", retries=0)


def test_lint_flags_a_literal_webhook_and_a_missing_check():
    yaml_text = """
name: t
steps:
  - id: read
    use: csv.read
    with: { path: orders.csv }
  - id: post
    use: discord.send
    input: read
    with: { webhook: "https://discord.com/api/webhooks/1/abc", content: "{id}" }
"""
    codes = {f.code for f in check_text(yaml_text).findings}
    assert "AL003" in codes  # secret written in the pipeline
    ok = yaml_text.replace('"https://discord.com/api/webhooks/1/abc"', "${DISCORD_WEBHOOK}")
    assert "AL003" not in {f.code for f in check_text(ok).findings}
