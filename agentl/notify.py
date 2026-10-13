"""Notifications: rules subscribe to runner events and post to channels."""

from __future__ import annotations

import json
import logging
import operator
import re
import smtplib
import ssl
import urllib.request
from collections.abc import Callable
from email.message import EmailMessage

from .spec import Channel, NotifySpec, Rule

log = logging.getLogger("agentl.notify")

_OPS = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}
_WHEN = re.compile(r"^\s*(\w+)\s*(>=|<=|==|!=|>|<)\s*(-?[\d.]+)\s*$")


def matches(rule: Rule, event: dict) -> bool:
    kinds = [rule.on] if isinstance(rule.on, str) else rule.on
    if event["type"] not in kinds and "*" not in kinds:
        return False
    if not rule.when:
        return True
    m = _WHEN.match(rule.when)
    if not m:
        raise ValueError(f"invalid 'when': {rule.when!r} (expected e.g. 'rate > 0.05')")
    field, op, number = m.groups()
    value = event.get("data", {}).get(field)
    return value is not None and _OPS[op](float(value), float(number))


def format_text(event: dict, pipeline: str) -> str:
    data = event.get("data", {})
    step = f" step {event['step']}" if event.get("step") else ""
    details = ", ".join(f"{k}={v}" for k, v in data.items() if k not in ("error", "summary"))
    lines = [f"[agentl] {pipeline}: {event['type']}{step}"]
    if details:
        lines.append(details)
    if data.get("error"):
        lines.append(f"error: {data['error']}")
    if data.get("summary"):
        lines.append(data["summary"])
    return "\n".join(lines)


def _post(url: str, payload: dict, headers: dict | None = None) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, default=str).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10).close()


def _failed(event: dict) -> bool:
    return event["type"].endswith(".failed") or event["type"] == "budget.exceeded"


def _title(event: dict) -> str:
    step = f" ({event['step']})" if event.get("step") else ""
    return f"agentl {event.get('pipeline', '')}: {event['type']}{step}"


def _slack(cfg: dict, text: str, event: dict) -> None:
    _post(cfg["webhook"], {"text": text})


def _webhook(cfg: dict, text: str, event: dict) -> None:
    _post(cfg["url"], {"text": text, "event": event}, cfg.get("headers"))


def _console(cfg: dict, text: str, event: dict) -> None:
    print(text)


def _discord(cfg: dict, text: str, event: dict) -> None:
    _post(cfg["webhook"], {"content": text[:2000]})


def _teams(cfg: dict, text: str, event: dict) -> None:
    """Teams Workflows webhook ("When a Teams webhook request is received") with an Adaptive Card."""
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": _title(event),
                "weight": "Bolder",
                "wrap": True,
                "color": "Attention" if _failed(event) else "Default",
            },
            {"type": "TextBlock", "text": text, "wrap": True, "fontType": "Monospace"},
        ],
    }
    attachment = {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
    _post(cfg["webhook"], {"type": "message", "attachments": [attachment]})


def _telegram(cfg: dict, text: str, event: dict) -> None:
    base = cfg.get("api_url", "https://api.telegram.org").rstrip("/")
    _post(f"{base}/bot{cfg['token']}/sendMessage", {"chat_id": cfg["chat_id"], "text": text[:4096]})


def _pagerduty(cfg: dict, text: str, event: dict) -> None:
    """PagerDuty Events API v2. Failures trigger; `run.succeeded` resolves the run's open alerts
    when `resolve: true` and a rule sends it here."""
    url = cfg.get("api_url", "https://events.pagerduty.com/v2/enqueue")
    dedup = f"agentl-{event.get('pipeline', '')}-{event.get('step') or 'run'}"
    if event["type"] == "run.succeeded":
        _post(url, {"routing_key": cfg["routing_key"], "event_action": "resolve", "dedup_key": dedup})
        return
    _post(
        url,
        {
            "routing_key": cfg["routing_key"],
            "event_action": "trigger",
            "dedup_key": dedup,
            "payload": {
                "summary": _title(event)[:1024],
                "source": f"agentl/{event.get('pipeline', '')}",
                "severity": cfg.get("severity", "error" if _failed(event) else "warning"),
                "custom_details": {"text": text, **event.get("data", {})},
            },
        },
    )


def _opsgenie(cfg: dict, text: str, event: dict) -> None:
    region = "api.eu.opsgenie.com" if cfg.get("region") == "eu" else "api.opsgenie.com"
    url = cfg.get("api_url", f"https://{region}/v2/alerts")
    _post(
        url,
        {
            "message": _title(event)[:130],
            "alias": f"agentl-{event.get('pipeline', '')}-{event.get('step') or 'run'}",
            "description": text[:15000],
            "priority": cfg.get("priority", "P2" if _failed(event) else "P4"),
            "source": "agentl",
            "details": {k: str(v) for k, v in event.get("data", {}).items()},
        },
        {"Authorization": f"GenieKey {cfg['api_key']}"},
    )


def _email(cfg: dict, text: str, event: dict) -> None:
    """SMTP. cfg: host, port (587), user, password, from, to (str or list), starttls (true), ssl (false)."""
    msg = EmailMessage()
    msg["Subject"] = _title(event)
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


ChannelSender = Callable[[dict, str, dict], None]

CHANNELS: dict[str, ChannelSender] = {
    "console": _console,
    "slack": _slack,
    "webhook": _webhook,
    "discord": _discord,
    "teams": _teams,
    "telegram": _telegram,
    "pagerduty": _pagerduty,
    "opsgenie": _opsgenie,
    "email": _email,
}


def register_channel(kind: str, sender: ChannelSender) -> None:
    """Add a channel type: `sender(config, text, event)`; config is the channel's YAML mapping."""
    CHANNELS[kind] = sender


def send(channel_name: str, channel: Channel, text: str, event: dict) -> None:
    kind = channel.type or channel_name
    if kind not in CHANNELS:
        raise ValueError(f"unknown notification channel type '{kind}'. Available: {sorted(CHANNELS)}")
    CHANNELS[kind](channel.model_extra or {}, text, event)


class Notifier:
    def __init__(
        self, spec: NotifySpec | None, pipeline: str, summarize: Callable[[dict], str] | None = None
    ):
        self.spec = spec or NotifySpec()
        self.pipeline = pipeline
        self.summarize = summarize
        self._sent: set[tuple[str, str, str]] = set()

    def __call__(self, event: dict) -> None:
        for rule in self.spec.rules:
            try:
                if not matches(rule, event):
                    continue
                key = (event["type"], event.get("step") or "", json.dumps(rule.to))
                if key in self._sent:  # one message per rule/step per run
                    continue
                self._sent.add(key)
                if rule.summary == "agent" and self.summarize:
                    event = {**event, "data": {**event.get("data", {}), "summary": self.summarize(event)}}
                text = format_text(event, self.pipeline)
                for name in rule.to:
                    if name not in self.spec.channels:
                        log.warning("notify: unknown channel %s", name)
                        continue
                    send(name, self.spec.channels[name], text, event)
            except Exception as e:  # a broken channel must never fail the pipeline
                log.warning("notify failed: %s", e)
