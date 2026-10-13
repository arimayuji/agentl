"""Notifications: rules subscribe to runner events and post to channels."""

from __future__ import annotations

import json
import logging
import operator
import re
import urllib.request
from typing import Any, Callable

from .spec import Channel, NotifySpec, Rule

log = logging.getLogger("agentl.notify")

_OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "==": operator.eq, "!=": operator.ne}
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


def _post(url: str, payload: dict) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload, default=str).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=10).close()


def send(channel_name: str, channel: Channel, text: str, event: dict) -> None:
    kind = channel.type or channel_name
    cfg = channel.model_extra or {}
    if kind == "slack":
        _post(cfg["webhook"], {"text": text})
    elif kind == "webhook":
        _post(cfg["url"], {"text": text, "event": event})
    elif kind == "console":
        print(text)
    else:
        raise ValueError(f"unknown notification channel type '{kind}'")


class Notifier:
    def __init__(self, spec: NotifySpec | None, pipeline: str,
                 summarize: Callable[[dict], str] | None = None):
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
