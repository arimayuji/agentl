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


def _compact(record: dict) -> str:
    return "\n".join(f"**{k}**: {fill('{' + k + '}', record)}" for k in record)


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
