"""Chat destinations as pipeline steps: `discord.send` posts records to a channel through an
incoming webhook (#145).

Records are untrusted data, so mentions are off: a record with `@everyone` in it is posted as
text, without pinging anyone. Templates fill `{field}` placeholders only (no format specs or
attribute access), and `max_messages` caps one run so a bad input cannot flood a channel."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .. import netguard
from ..registry import connector
from ._util import single

DISCORD_LIMIT = 2000  # characters per message
EMBED_DESCRIPTION_LIMIT = 4096
_FIELD = re.compile(r"\{(\w+)\}")


def fill(template: str, record: dict) -> str:
    """`{field}` placeholders replaced by the record's values; unknown fields stay as written."""

    def value(m: re.Match) -> str:
        if m.group(1) not in record:
            return m.group(0)
        v = record[m.group(1)]
        return (
            "" if v is None else v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False)
        )

    return _FIELD.sub(value, template)


def _compact(record: dict, bold: bool = True) -> str:
    """`key: value` lines; keys in bold where the service renders Markdown (Discord)."""
    mark = "**" if bold else ""
    return "\n".join(f"{mark}{k}{mark}: {fill('{' + k + '}', record)}" for k in record)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _batches(texts: list[str], limit: int) -> list[str]:
    """Texts joined by blank lines into as few messages as fit `limit`."""
    out: list[str] = []
    for t in texts:
        t = _cut(t, limit)
        if out and len(out[-1]) + 2 + len(t) <= limit:
            out[-1] += "\n\n" + t
        else:
            out.append(t)
    return out


def _post(url: str, payload: dict, timeout: float, retries: int) -> dict:
    """POST with Discord's rate limit honoured: on 429 wait `retry_after`, on 5xx back off."""
    data = json.dumps(payload, default=str).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with netguard.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if attempt < retries and e.code == 429:
                try:
                    wait = float(json.loads(detail or "{}").get("retry_after", 1))
                except ValueError:
                    wait = 1.0
                time.sleep(min(wait, 30))
                continue
            if attempt < retries and e.code >= 500:
                time.sleep(2**attempt)
                continue
            raise RuntimeError(f"discord.send: HTTP {e.code}: {detail}") from None
    raise AssertionError("unreachable")


@connector("discord.send")
def discord_send(
    input: Any = None,
    webhook: str = "",
    content: str | None = None,
    embed: dict | None = None,
    mode: str = "each",
    username: str | None = None,
    avatar_url: str | None = None,
    max_messages: int = 50,
    timeout: float = 30,
    retries: int = 3,
) -> list[dict]:
    """Posts each record (`mode: each`) or all records packed into few messages (`mode: batch`)
    to the Discord webhook. `content` and `embed` (title, description, url, color, footer) are
    templates with `{field}` placeholders; with neither, each record is shown as `key: value`
    lines. Returns one record per message with Discord's message id."""
    if not webhook:
        raise ValueError("discord.send needs `webhook` (use ${DISCORD_WEBHOOK}, never the URL itself)")
    netguard.require_http(webhook, "discord webhook")
    if mode not in ("each", "batch"):
        raise ValueError(f"discord.send `mode` must be each or batch, got {mode!r}")
    if embed and mode == "batch":
        raise ValueError("discord.send: `embed` posts one message per record; use mode: each")
    records = single(input)
    if not records:
        return []

    base: dict[str, Any] = {"allowed_mentions": {"parse": []}}
    if username:
        base["username"] = username
    if avatar_url:
        base["avatar_url"] = avatar_url

    payloads: list[dict] = []
    if mode == "batch":
        texts = [fill(content, r) if content else _compact(r) for r in records]
        payloads = [{**base, "content": m} for m in _batches(texts, DISCORD_LIMIT)]
    else:
        for r in records:
            p = dict(base)
            if content or not embed:
                p["content"] = _cut(fill(content, r) if content else _compact(r), DISCORD_LIMIT)
            if embed:
                e: dict[str, Any] = {}
                for key in ("title", "description", "url"):
                    if embed.get(key):
                        e[key] = fill(str(embed[key]), r)
                if "description" in e:
                    e["description"] = _cut(e["description"], EMBED_DESCRIPTION_LIMIT)
                if "title" in e:
                    e["title"] = _cut(e["title"], 256)
                if embed.get("color") is not None:
                    c = embed["color"]
                    e["color"] = int(c.lstrip("#"), 16) if isinstance(c, str) else int(c)
                if embed.get("footer"):
                    e["footer"] = {"text": _cut(fill(str(embed["footer"]), r), 2048)}
                p["embeds"] = [e]
            payloads.append(p)

    if len(payloads) > max_messages:
        raise ValueError(
            f"discord.send would post {len(payloads)} messages, over max_messages={max_messages}. "
            "Use mode: batch, filter the records first, or raise max_messages"
        )

    sep = "&" if urllib.parse.urlsplit(webhook).query else "?"
    url = f"{webhook}{sep}wait=true"  # Discord returns the created message
    out = []
    for i, p in enumerate(payloads):
        msg = _post(url, p, timeout, retries)
        out.append(
            {"message": i + 1, "id": msg.get("id"), "channel_id": msg.get("channel_id"), "status": "sent"}
        )
    return out


# ---------------------------------------------------------------- every other messaging service
# One sender per service, `send(cfg, text, title) -> dict`, shared by the `<service>.send` steps
# below and by the `notify:` channels of the same name (#146). `cfg` is the step's `with` (or the
# channel's YAML mapping) minus the generic keys.


def _request(
    url: str,
    *,
    json_body: Any = None,
    form: dict | None = None,
    raw: bytes | None = None,
    headers: dict | None = None,
    auth: tuple[str, str] | None = None,
    timeout: float = 30,
    retries: int = 3,
) -> dict:
    """POST through the network guard; 429 waits `Retry-After` (or `retry_after`), 5xx backs off."""
    import base64

    netguard.require_http(url, "messaging url")
    hdrs = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body, default=str).encode()
        hdrs.setdefault("Content-Type", "application/json")
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    else:
        data = raw or b""
    if auth:
        hdrs["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
        try:
            with netguard.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                try:
                    out = json.loads(body) if body else {}
                except ValueError:
                    out = {}
                return out if isinstance(out, dict) else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if attempt < retries and e.code == 429:
                wait = e.headers.get("Retry-After") if e.headers else None
                try:
                    secs = float(wait or json.loads(detail or "{}").get("retry_after", 1))
                except (ValueError, AttributeError):
                    secs = 1.0
                time.sleep(min(secs, 30))
                continue
            if attempt < retries and e.code >= 500:
                time.sleep(2**attempt)
                continue
            raise RuntimeError(f"HTTP {e.code} from {urllib.parse.urlsplit(url).netloc}: {detail}") from None
    raise AssertionError("unreachable")


def _need(service: str, cfg: dict, *keys: str) -> None:
    missing = [k for k in keys if not cfg.get(k)]
    if missing:
        raise ValueError(f"{service} needs {', '.join(missing)} (put secrets in ${{ENV}} variables)")


def _slack(cfg: dict, text: str, title: str) -> dict:
    """Slack (and Mattermost, Rocket.Chat): incoming webhook. `<!channel>`-style mentions in data
    are escaped so records cannot ping a channel."""
    _need("slack", cfg, "webhook")
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    payload: dict[str, Any] = {"text": safe}
    for k in ("username", "icon_emoji", "icon_url", "channel"):
        if cfg.get(k):
            payload[k] = cfg[k]
    _request(cfg["webhook"], json_body=payload, **_net(cfg))
    return {}


def _teams(cfg: dict, text: str, title: str) -> dict:
    """Teams Workflows webhook ("When a Teams webhook request is received") with an Adaptive Card."""
    _need("teams", cfg, "webhook")
    body: list[dict] = []
    if title:
        body.append({"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True})
    body.append({"type": "TextBlock", "text": text, "wrap": True})
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
    }
    attachment = {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
    _request(cfg["webhook"], json_body={"type": "message", "attachments": [attachment]}, **_net(cfg))
    return {}


def _telegram(cfg: dict, text: str, title: str) -> dict:
    """Telegram Bot API sendMessage: `token` (from @BotFather) and `chat_id`. Plain text, so data
    cannot inject Markdown or HTML."""
    _need("telegram", cfg, "token", "chat_id")
    base = str(cfg.get("api_url", "https://api.telegram.org")).rstrip("/")
    out = _request(
        f"{base}/bot{cfg['token']}/sendMessage",
        json_body={"chat_id": cfg["chat_id"], "text": text, "disable_web_page_preview": True},
        **_net(cfg),
    )
    return {"id": (out.get("result") or {}).get("message_id")}


def _googlechat(cfg: dict, text: str, title: str) -> dict:
    """Google Chat space incoming webhook. `<users/all>` in data is defused (no @all)."""
    _need("googlechat", cfg, "webhook")
    out = _request(cfg["webhook"], json_body={"text": text.replace("<users/", "< users/")}, **_net(cfg))
    return {"id": out.get("name")}


def _whatsapp(cfg: dict, text: str, title: str) -> dict:
    """WhatsApp Cloud API (Meta): `token`, `phone_number_id`, `to` (with country code, e.g.
    5511999999999). Free text is delivered only within 24 hours of the person's last message to
    the number; otherwise set `template: {name, language}` (an approved template) and the text
    fills its first body parameter."""
    _need("whatsapp", cfg, "token", "phone_number_id", "to")
    base = str(cfg.get("api_url", "https://graph.facebook.com/v21.0")).rstrip("/")
    to = str(cfg["to"]).lstrip("+").replace(" ", "")
    payload: dict[str, Any] = {"messaging_product": "whatsapp", "to": to}
    tpl = cfg.get("template")
    if tpl:
        name = tpl if isinstance(tpl, str) else tpl.get("name")
        lang = "pt_BR" if isinstance(tpl, str) else tpl.get("language", "pt_BR")
        t: dict[str, Any] = {"name": name, "language": {"code": lang}}
        if text:
            t["components"] = [{"type": "body", "parameters": [{"type": "text", "text": text}]}]
        payload.update(type="template", template=t)
    else:
        payload.update(type="text", text={"body": text, "preview_url": False})
    out = _request(
        f"{base}/{cfg['phone_number_id']}/messages",
        json_body=payload,
        headers={"Authorization": f"Bearer {cfg['token']}"},
        **_net(cfg),
    )
    return {"id": ((out.get("messages") or [{}])[0]).get("id")}


def _sms(cfg: dict, text: str, title: str) -> dict:
    """Twilio Messages API: `account_sid`, `auth_token`, `from`, `to`. `to: "whatsapp:+55..."`
    (with a WhatsApp-enabled `from`) sends through WhatsApp instead."""
    _need("sms", cfg, "account_sid", "auth_token", "from", "to")
    base = str(cfg.get("api_url", "https://api.twilio.com")).rstrip("/")
    out = _request(
        f"{base}/2010-04-01/Accounts/{cfg['account_sid']}/Messages.json",
        form={"From": cfg["from"], "To": cfg["to"], "Body": text},
        auth=(cfg["account_sid"], cfg["auth_token"]),
        **_net(cfg),
    )
    return {"id": out.get("sid")}


def _ntfy(cfg: dict, text: str, title: str) -> dict:
    """ntfy (ntfy.sh or self-hosted): `topic` URL like https://ntfy.sh/my-topic; optional
    `token`, `priority` (1-5), `tags`."""
    _need("ntfy", cfg, "topic")
    headers: dict[str, str] = {"Content-Type": "text/plain; charset=utf-8"}
    if title:
        headers["Title"] = title.encode("utf-8").decode("latin-1", "replace")[:250]
    if cfg.get("priority"):
        headers["Priority"] = str(cfg["priority"])
    if cfg.get("tags"):
        tags = cfg["tags"]
        headers["Tags"] = ",".join(tags) if isinstance(tags, list) else str(tags)
    if cfg.get("token"):
        headers["Authorization"] = f"Bearer {cfg['token']}"
    out = _request(str(cfg["topic"]), raw=text.encode(), headers=headers, **_net(cfg))
    return {"id": out.get("id")}


def _pushover(cfg: dict, text: str, title: str) -> dict:
    """Pushover: application `token` and `user` key; optional `priority`, `device`, `sound`."""
    _need("pushover", cfg, "token", "user")
    url = cfg.get("api_url", "https://api.pushover.net/1/messages.json")
    form = {"token": cfg["token"], "user": cfg["user"], "message": text}
    if title:
        form["title"] = title[:250]
    for k in ("priority", "device", "sound", "url"):
        if cfg.get(k) is not None:
            form[k] = str(cfg[k])
    out = _request(url, form=form, **_net(cfg))
    return {"id": out.get("request")}


def _matrix(cfg: dict, text: str, title: str) -> dict:
    """Matrix (Element and others): `homeserver` (https://matrix.org), `access_token`, `room_id`."""
    import uuid

    _need("matrix", cfg, "homeserver", "access_token", "room_id")
    base = str(cfg["homeserver"]).rstrip("/")
    room = urllib.parse.quote(str(cfg["room_id"]), safe="")
    url = f"{base}/_matrix/client/v3/rooms/{room}/send/m.room.message/{uuid.uuid4().hex}"
    req_headers = {"Authorization": f"Bearer {cfg['access_token']}", "Content-Type": "application/json"}
    data = json.dumps({"msgtype": "m.text", "body": text}).encode()
    netguard.require_http(url, "matrix homeserver")
    req = urllib.request.Request(url, data=data, headers=req_headers, method="PUT")  # Matrix sends with PUT
    try:
        with netguard.urlopen(req, timeout=float(cfg.get("timeout", 30))) as resp:
            out = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"HTTP {e.code} from matrix: {e.read().decode('utf-8', 'replace')[:300]}"
        ) from None
    return {"id": out.get("event_id")}


def _email(cfg: dict, text: str, title: str) -> dict:
    """SMTP: host, port (587), user, password, from, to (str or list), starttls (true), ssl (false)."""
    import smtplib
    import ssl
    from email.message import EmailMessage

    _need("email", cfg, "host", "to")
    msg = EmailMessage()
    msg["Subject"] = title or "agentl"
    msg["From"] = cfg.get("from") or cfg.get("user")
    to = cfg["to"] if isinstance(cfg["to"], list) else [cfg["to"]]
    msg["To"] = ", ".join(to)
    msg.set_content(text)
    port = int(cfg.get("port", 465 if cfg.get("ssl") else 587))
    smtp_cls = smtplib.SMTP_SSL if cfg.get("ssl") else smtplib.SMTP
    with smtp_cls(cfg["host"], port, timeout=20) as smtp:
        if not cfg.get("ssl") and cfg.get("starttls", True):
            smtp.starttls(context=ssl.create_default_context())
        if cfg.get("user"):
            smtp.login(cfg["user"], cfg["password"])
        smtp.send_message(msg)
    return {}


def _net(cfg: dict) -> dict:
    return {"timeout": float(cfg.get("timeout", 30)), "retries": int(cfg.get("retries", 3))}


Sender = Any  # Callable[[dict, str, str], dict]

# service -> (sender, message limit in characters)
SERVICES: dict[str, tuple[Sender, int]] = {
    "slack": (_slack, 40000),
    "mattermost": (_slack, 16000),
    "teams": (_teams, 20000),
    "telegram": (_telegram, 4096),
    "googlechat": (_googlechat, 4096),
    "whatsapp": (_whatsapp, 4096),
    "sms": (_sms, 1600),
    "ntfy": (_ntfy, 4096),
    "pushover": (_pushover, 1024),
    "matrix": (_matrix, 30000),
    "email": (_email, 1_000_000),
}
_GENERIC = {"content", "title", "mode", "max_messages"}


def send_records(service: str, records: list[dict], options: dict) -> list[dict]:
    """The shared body of every `<service>.send` step: templates, each/batch, the flood cap."""
    sender, limit = SERVICES[service]
    content = options.get("content")
    title = options.get("title") or ""
    mode = options.get("mode", "each")
    max_messages = int(options.get("max_messages", 50))
    cfg = {k: v for k, v in options.items() if k not in _GENERIC}
    if mode not in ("each", "batch"):
        raise ValueError(f"{service}.send `mode` must be each or batch, got {mode!r}")
    texts = [fill(content, r) if content else _compact(r, bold=False) for r in records]
    messages = (
        [(_cut(t, limit), fill(title, r)) for t, r in zip(texts, records, strict=True)]
        if mode == "each"
        else [(m, title) for m in _batches(texts, limit)]
    )
    if len(messages) > max_messages:
        raise ValueError(
            f"{service}.send would send {len(messages)} messages, over max_messages={max_messages}. "
            "Use mode: batch, filter the records first, or raise max_messages"
        )
    out = []
    for i, (text, ttl) in enumerate(messages):
        info = sender(cfg, text, ttl) or {}
        out.append({"message": i + 1, "id": info.get("id"), "status": "sent"})
    return out


def _make_step(service: str) -> Any:
    def step(input: Any = None, **options: Any) -> list[dict]:
        records = single(input)
        return send_records(service, records, options) if records else []

    step.__name__ = f"{service}_send"
    step.__doc__ = (SERVICES[service][0].__doc__ or "").strip()
    return step


for _service in SERVICES:
    connector(f"{_service}.send")(_make_step(_service))
