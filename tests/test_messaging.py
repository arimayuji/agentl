"""Every `<service>.send` step and its notify channel against one local fake server (#146)."""

import base64
import http.server
import json
import threading
import urllib.parse
from typing import ClassVar

import pytest

from agentl.connectors import chat
from agentl.lint import _DELIVERING
from agentl.notify import CHANNELS
from agentl.notify import send as notify
from agentl.registry import get
from agentl.spec import Channel


class Api(http.server.BaseHTTPRequestHandler):
    """Records (method, path, headers, body) and answers like each service does."""

    log: ClassVar[list] = []
    fail: ClassVar[list] = []

    def log_message(self, *a):
        pass

    def _handle(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("application/json"):
            body = json.loads(raw)
        elif ctype.startswith("application/x-www-form-urlencoded"):
            body = dict(urllib.parse.parse_qsl(raw.decode()))
        else:
            body = raw.decode()
        if Api.fail:
            code = Api.fail.pop(0)
            self.send_response(code)
            if code == 429:
                self.send_header("Retry-After", "0")
            self.end_headers()
            return
        Api.log.append((self.command, self.path, dict(self.headers), body))
        answer = {
            "/bot": {"ok": True, "result": {"message_id": 7}},
            "/wa": {"messages": [{"id": "wamid.1"}]},
            "/2010": {"sid": "SM1"},
            "/_matrix": {"event_id": "$e1"},
            "/push": {"request": "req1"},
            "/chat": {"name": "spaces/x/messages/1"},
            "/topic": {"id": "n1"},
        }
        data = next((v for k, v in answer.items() if self.path.startswith(k)), {})
        out = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_POST = do_PUT = _handle


@pytest.fixture
def api():
    Api.log, Api.fail = [], []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Api)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


ORDERS = [{"id": 1, "customer": "Ana"}, {"id": 2, "customer": "Bruno"}]


def cfg_for(service, url):
    return {
        "slack": {"webhook": f"{url}/slack"},
        "mattermost": {"webhook": f"{url}/mm"},
        "teams": {"webhook": f"{url}/teams"},
        "telegram": {"token": "T0K", "chat_id": "-100", "api_url": url},
        "googlechat": {"webhook": f"{url}/chat"},
        "whatsapp": {
            "token": "WA",
            "phone_number_id": "123",
            "to": "+55 11999990000",
            "api_url": f"{url}/wa",
        },
        "sms": {
            "account_sid": "AC1",
            "auth_token": "AT",
            "from": "+15550000",
            "to": "+5511999990000",
            "api_url": url,
        },
        "ntfy": {"topic": f"{url}/topic", "token": "NT", "priority": 4, "tags": ["warning"]},
        "pushover": {"token": "PT", "user": "PU", "api_url": f"{url}/push"},
        "matrix": {"homeserver": url, "access_token": "MX", "room_id": "!room:matrix.org"},
    }[service]


HTTP_SERVICES = [s for s in chat.SERVICES if s != "email"]


@pytest.mark.parametrize("service", HTTP_SERVICES)
def test_each_record_is_one_message(api, service):
    out = get(f"{service}.send")(ORDERS, content="Order {id} from {customer}", **cfg_for(service, api))
    assert [o["message"] for o in out] == [1, 2] and all(o["status"] == "sent" for o in out)
    assert len(Api.log) == 2
    assert "Order 1 from Ana" in json.dumps(Api.log[0][3])


@pytest.mark.parametrize("service", HTTP_SERVICES)
def test_notify_channel_uses_the_same_sender(api, service):
    assert service in CHANNELS
    event = {"pipeline": "sales", "type": "run.failed", "data": {}}
    notify("c", Channel(type=service, **cfg_for(service, api)), "it broke", event)
    assert len(Api.log) == 1 and "it broke" in json.dumps(Api.log[0][3])


def test_service_payloads(api):
    send = lambda s, **kw: get(f"{s}.send")(ORDERS[:1], content="hi {customer}", **cfg_for(s, api), **kw)  # noqa: E731
    assert send("telegram")[0]["id"] == 7
    _, path, _, body = Api.log[-1]
    assert path == "/botT0K/sendMessage" and body["chat_id"] == "-100" and body["text"] == "hi Ana"

    assert send("whatsapp")[0]["id"] == "wamid.1"
    _, path, headers, body = Api.log[-1]
    assert path == "/wa/123/messages" and headers["Authorization"] == "Bearer WA"
    assert body == {"messaging_product": "whatsapp", "to": "5511999990000", "type": "text",
                    "text": {"body": "hi Ana", "preview_url": False}}  # fmt: skip

    send("whatsapp", template={"name": "order_update", "language": "pt_BR"})
    body = Api.log[-1][3]
    assert body["type"] == "template" and body["template"]["name"] == "order_update"
    assert body["template"]["components"][0]["parameters"] == [{"type": "text", "text": "hi Ana"}]

    assert send("sms")[0]["id"] == "SM1"
    _, path, headers, body = Api.log[-1]
    assert path == "/2010-04-01/Accounts/AC1/Messages.json"
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"AC1:AT").decode()
    assert body == {"From": "+15550000", "To": "+5511999990000", "Body": "hi Ana"}

    send("ntfy", title="Order {id}")
    _, path, headers, body = Api.log[-1]
    assert body == "hi Ana" and headers["Title"] == "Order 1" and headers["Priority"] == "4"
    assert headers["Tags"] == "warning" and headers["Authorization"] == "Bearer NT"

    assert send("pushover")[0]["id"] == "req1"
    assert Api.log[-1][3] == {"token": "PT", "user": "PU", "message": "hi Ana"}

    assert send("matrix")[0]["id"] == "$e1"
    method, path, headers, body = Api.log[-1]
    assert method == "PUT" and path.startswith(
        "/_matrix/client/v3/rooms/%21room%3Amatrix.org/send/m.room.message/"
    )
    assert body == {"msgtype": "m.text", "body": "hi Ana"} and headers["Authorization"] == "Bearer MX"

    send("teams", title="Order {id}")
    card = Api.log[-1][3]["attachments"][0]["content"]
    assert [b["text"] for b in card["body"]] == ["Order 1", "hi Ana"]


def test_mentions_from_data_are_defused(api):
    get("slack.send")([{"t": "<!channel> pay now"}], content="{t}", **cfg_for("slack", api))
    assert Api.log[-1][3]["text"] == "&lt;!channel&gt; pay now"
    get("googlechat.send")([{"t": "<users/all> hi"}], content="{t}", **cfg_for("googlechat", api))
    assert "<users/" not in Api.log[-1][3]["text"]


def test_batch_and_default_view(api):
    rows = [{"i": i, "note": "x" * 600} for i in range(6)]
    get("telegram.send")(rows, mode="batch", **cfg_for("telegram", api))
    texts = [b["text"] for _, _, _, b in Api.log]
    assert len(texts) < 6 and all(len(t) <= 4096 for t in texts)
    assert texts[0].startswith("i: 0\nnote: ")  # plain key: value, no Markdown


def test_flood_cap_and_bad_mode(api):
    with pytest.raises(ValueError, match="max_messages"):
        get("sms.send")([{"i": i} for i in range(51)], **cfg_for("sms", api))
    with pytest.raises(ValueError, match="mode"):
        get("sms.send")(ORDERS, mode="all", **cfg_for("sms", api))
    assert Api.log == []


def test_missing_settings_say_what_is_missing():
    with pytest.raises(ValueError, match="phone_number_id, to"):
        get("whatsapp.send")(ORDERS, token="x")
    assert get("whatsapp.send")([], token="x") == []


def test_retries_then_reports_the_error(api, monkeypatch):
    monkeypatch.setattr(chat.time, "sleep", lambda s: None)
    Api.fail = [429, 503]
    assert get("ntfy.send")(ORDERS[:1], **cfg_for("ntfy", api))[0]["status"] == "sent"
    Api.fail = [403]
    with pytest.raises(RuntimeError, match="HTTP 403"):
        get("ntfy.send")(ORDERS[:1], **cfg_for("ntfy", api))


def test_email_send(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def starttls(self, context):
            sent.append(("starttls",))

        def login(self, user, password):
            sent.append(("login", user))

        def send_message(self, msg):
            sent.append(("msg", msg["Subject"], msg["To"], msg.get_content().strip()))

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    get("email.send")(
        ORDERS[:1], host="smtp.x", user="u", password="p", to=["a@x", "b@x"],
        title="Order {id}", content="hi {customer}",
    )  # fmt: skip
    assert sent == [
        ("connect", "smtp.x", 587),
        ("starttls",),
        ("login", "u"),
        ("msg", "Order 1", "a@x, b@x", "hi Ana"),
    ]


def test_every_send_step_is_a_delivering_step():
    assert {f"{s}.send" for s in chat.SERVICES} | {"discord.send"} <= _DELIVERING
