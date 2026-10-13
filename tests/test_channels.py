import http.server
import json
import threading

import pytest

from agentl import register_channel
from agentl.notify import send
from agentl.spec import Channel

EVENT = {
    "run_id": "r1",
    "pipeline": "sales",
    "type": "step.failed",
    "step": "load",
    "data": {"error": "boom", "records": 3},
}


@pytest.fixture
def hook():
    got: list = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            got.append({"path": self.path, "headers": dict(self.headers), "body": body})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", got
    srv.shutdown()
    srv.server_close()


def ch(type, **cfg):
    return Channel(type=type, **cfg)


def test_discord_and_teams(hook):
    url, got = hook
    send("d", ch("discord", webhook=f"{url}/d"), "x" * 3000, EVENT)
    assert len(got[0]["body"]["content"]) == 2000
    send("t", ch("teams", webhook=f"{url}/t"), "text", EVENT)
    card = got[1]["body"]["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    assert card["body"][0]["text"] == "agentl sales: step.failed (load)"
    assert card["body"][0]["color"] == "Attention"


def test_telegram(hook):
    url, got = hook
    send("tg", ch("telegram", token="T0K", chat_id="-100", api_url=url), "hi", EVENT)
    assert got[0]["path"] == "/botT0K/sendMessage"
    assert got[0]["body"] == {"chat_id": "-100", "text": "hi"}


def test_pagerduty_trigger_and_resolve(hook):
    url, got = hook
    send("pd", ch("pagerduty", routing_key="RK", api_url=url), "hi", EVENT)
    body = got[0]["body"]
    assert body["event_action"] == "trigger" and body["routing_key"] == "RK"
    assert body["dedup_key"] == "agentl-sales-load"
    assert body["payload"]["severity"] == "error" and body["payload"]["custom_details"]["error"] == "boom"
    send(
        "pd",
        ch("pagerduty", routing_key="RK", api_url=url),
        "ok",
        {**EVENT, "type": "run.succeeded", "step": None},
    )
    assert got[1]["body"]["event_action"] == "resolve"


def test_opsgenie(hook):
    url, got = hook
    send("og", ch("opsgenie", api_key="K", api_url=url), "hi", EVENT)
    assert got[0]["headers"]["Authorization"] == "GenieKey K"
    assert got[0]["body"]["priority"] == "P2" and got[0]["body"]["alias"] == "agentl-sales-load"


def test_email(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["addr"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def starttls(self, context):
            sent["tls"] = True

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, msg):
            sent["msg"] = msg

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    cfg = {"host": "smtp.x", "user": "u@x", "password": "p", "to": ["a@x", "b@x"]}
    send("mail", ch("email", **cfg), "body text", EVENT)
    assert sent["addr"] == ("smtp.x", 587) and sent["tls"] and sent["login"] == ("u@x", "p")
    msg = sent["msg"]
    assert msg["To"] == "a@x, b@x" and msg["From"] == "u@x"
    assert msg["Subject"] == "agentl sales: step.failed (load)"
    assert "body text" in msg.get_content()


def test_custom_channel_and_unknown():
    seen = []
    register_channel("mine", lambda cfg, text, event: seen.append((cfg["x"], text)))
    send("m", ch("mine", x=1), "hello", EVENT)
    assert seen == [(1, "hello")]
    with pytest.raises(ValueError, match="Available"):
        send("z", ch("nope"), "x", EVENT)
