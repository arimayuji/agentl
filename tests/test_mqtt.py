"""MQTT (#226): a fake paho client records what agentl asks of it; no broker runs here."""

import json
import types
from typing import ClassVar

import pytest

from agentl import parse
from agentl.connectors import queues
from agentl.registry import get
from agentl.runner import Runner
from agentl.store import Store


class RC:
    def __init__(self, value=0):
        self.value = value

    @property
    def is_failure(self):
        return self.value >= 0x80

    def __str__(self):
        return f"rc{self.value}"


class Info:
    def __init__(self, ok):
        self.ok = ok

    def wait_for_publish(self, timeout=None):
        pass

    def is_published(self):
        return self.ok


class FakeClient:
    log: ClassVar[list] = []
    pending: ClassVar[list] = []
    connect_rc: ClassVar[int] = 0
    suback: ClassVar[int] = 1
    confirm: ClassVar[bool] = True

    def __init__(self, api, client_id="", clean_session=True, manual_ack=False):
        FakeClient.log.append(("init", client_id, clean_session, manual_ack))

    def username_pw_set(self, user, pwd):
        FakeClient.log.append(("auth", user, pwd))

    def tls_set(self):
        FakeClient.log.append(("tls",))

    def connect(self, host, port, keepalive):
        FakeClient.log.append(("connect", host, port))

    def loop_start(self):
        self.on_connect(self, None, {}, RC(FakeClient.connect_rc), None)

    def loop_stop(self):
        FakeClient.log.append(("loop_stop",))

    def subscribe(self, topics):
        FakeClient.log.append(("subscribe", topics))
        self.on_subscribe(self, None, 1, [RC(FakeClient.suback)], None)
        for m in FakeClient.pending:
            self.on_message(self, None, m)

    def ack(self, mid, qos):
        FakeClient.log.append(("ack", mid, qos))

    def disconnect(self):
        FakeClient.log.append(("disconnect",))

    def publish(self, topic, payload, qos, retain):
        FakeClient.log.append(("publish", topic, payload, qos, retain))
        return Info(FakeClient.confirm)


def msg(mid, payload, topic="sensors/d1/temp", qos=1, retain=False):
    return types.SimpleNamespace(mid=mid, payload=payload, topic=topic, qos=qos, retain=retain)


FakePaho = types.SimpleNamespace(Client=FakeClient, CallbackAPIVersion=types.SimpleNamespace(VERSION2="v2"))


@pytest.fixture
def paho(monkeypatch):
    FakeClient.log.clear()
    FakeClient.connect_rc, FakeClient.suback, FakeClient.confirm = 0, 1, True
    FakeClient.pending[:] = [msg(1, b'{"t": 21.5}'), msg(2, b"on", qos=0, retain=True), msg(3, b'{"t": 22}')]
    monkeypatch.setattr(queues, "require", lambda module, extra: FakePaho)


SAVED: list = []


def load(rows):
    SAVED.extend(rows)
    if any(isinstance(r["value"], dict) and r["value"].get("t") == 99 for r in rows):
        raise RuntimeError("load failed")
    return rows


def run(tmp_path, args):
    SAVED.clear()
    spec = parse(
        {
            "name": "iot",
            "steps": [
                {"id": "read", "use": "mqtt.subscribe", "with": args},
                {"id": "load", "fn": "tests.test_mqtt:load", "input": "read"},
            ],
        }
    )
    with Store(tmp_path / "s.db") as store:
        return Runner(spec, store=store).run()


def test_subscribe_batch_records_and_session(paho):
    rows = get("mqtt.subscribe")(
        None,
        url="mqtts://dev:s3cret@broker:8883",
        topic=["sensors/+/temp", "alerts/#"],
        client_id="etl-1",
        max_records=2,
        max_seconds=1,
    )
    assert [r["value"] for r in rows] == [{"t": 21.5}, "on"]
    assert rows[1]["retain"] is True and rows[1]["qos"] == 0 and rows[0]["topic"] == "sensors/d1/temp"
    assert rows[0]["timestamp"].endswith("+00:00")
    log = FakeClient.log
    assert log[0] == ("init", "etl-1", False, True)  # persistent session, manual acks
    assert ("auth", "dev", "s3cret") in log and ("tls",) in log and ("connect", "broker", 8883) in log
    assert ("subscribe", [("sensors/+/temp", 1), ("alerts/#", 1)]) in log
    assert [e for e in log if e[0] == "ack"] == [("ack", 1, 1)]  # QoS 0 needs no ack; mid 3 left over


def test_acks_only_after_success(paho, tmp_path):
    r = run(tmp_path, {"url": "mqtt://broker", "topic": "sensors/#", "client_id": "c", "max_seconds": 0.3})
    assert r.status == "succeeded" and len(SAVED) == 3
    assert [e for e in FakeClient.log if e[0] == "ack"] == [("ack", 1, 1), ("ack", 3, 1)]
    assert FakeClient.log[-2:] == [("disconnect",), ("loop_stop",)]

    FakeClient.log.clear()
    FakeClient.pending[:] = [msg(7, b'{"t": 99}')]
    r = run(tmp_path, {"url": "mqtt://broker", "topic": "sensors/#", "client_id": "c", "max_seconds": 0.3})
    assert r.status == "failed" and not any(e[0] == "ack" for e in FakeClient.log)
    assert ("disconnect",) in FakeClient.log


def test_subscribe_validation_and_failures(paho):
    sub = get("mqtt.subscribe")
    with pytest.raises(ValueError, match="qos"):
        sub(None, url="mqtt://b", topic="t", qos=3)
    with pytest.raises(ValueError, match="client_id"):
        sub(None, url="mqtt://b", topic="t", clean_session=False)
    with pytest.raises(ValueError, match="url"):
        sub(None, url="http://b", topic="t")
    FakeClient.connect_rc = 0x87
    with pytest.raises(RuntimeError, match="cannot connect"):
        sub(None, url="mqtt://b", topic="t")
    FakeClient.connect_rc, FakeClient.suback = 0, 0x80
    with pytest.raises(RuntimeError, match="refused"):
        sub(None, url="mqtt://b", topic="t", max_seconds=0.1)
    FakeClient.suback = 0
    rows = sub(None, url="mqtt://b", topic="t", format="text", max_seconds=0.2)
    assert rows[0]["value"] == '{"t": 21.5}' and ("init", "", True, True) in FakeClient.log


def test_publish(paho):
    rows = [{"device": "d1", "t": 21.5}, {"device": "d2", "t": 19}]
    out = get("mqtt.publish")(rows, url="mqtt://b", topic_field="device", qos=1, retain=True)
    pubs = [e for e in FakeClient.log if e[0] == "publish"]
    assert pubs[0] == ("publish", "d1", json.dumps(rows[0]).encode(), 1, True) and len(pubs) == 2
    assert out == [{"topic": "{device}", "published": 2}]
    FakeClient.confirm = False
    with pytest.raises(RuntimeError, match="not confirmed"):
        get("mqtt.publish")(rows, url="mqtt://b", topic="out")
    assert FakeClient.log[-1] == ("loop_stop",)


def test_docs_recipe_up_to_aggregate(paho, tmp_path):
    FakeClient.pending[:] = [
        msg(i, json.dumps({"device": d, "ts": ts, "temperature": t}).encode())
        for i, (d, ts, t) in enumerate(
            [
                ("d1", "2026-10-17T10:00:05Z", "21.5"),
                ("d1", "2026-10-17T10:00:35Z", "22.5"),
                ("d2", "2026-10-17T10:01:00Z", "19"),
                ("d2", "bad", "1"),
            ],
            1,
        )
    ]
    spec = parse(
        {
            "name": "sensor-minutes",
            "steps": [
                {
                    "id": "readings",
                    "use": "mqtt.subscribe",
                    "with": {
                        "url": "mqtt://b",
                        "topic": "sensors/+/telemetry",
                        "client_id": "c",
                        "max_seconds": 0.3,
                    },
                },
                {"id": "fields", "use": "flatten", "input": "readings", "with": {"fields": "value"}},
                {
                    "id": "pick",
                    "use": "select",
                    "input": "fields",
                    "with": {
                        "fields": ["value.device", "value.ts", "value.temperature"],
                        "rename": {
                            "value.device": "device",
                            "value.ts": "ts",
                            "value.temperature": "temperature",
                        },
                    },
                },
                {
                    "id": "typed",
                    "use": "cast",
                    "input": "pick",
                    "with": {"fields": {"ts": "datetime", "temperature": "float"}},
                },
                {
                    "id": "per_minute",
                    "use": "aggregate",
                    "input": "typed",
                    "with": {
                        "group_by": "device",
                        "window": {"field": "ts", "every": "1m"},
                        "metrics": {"avg_temp": "avg(temperature)", "readings": "count()"},
                    },
                },
            ],
        }
    )
    with Store(tmp_path / "s.db") as store:
        r = Runner(spec, store=store).run()
    assert r.status == "succeeded"
    assert r.outputs["per_minute"] == [
        {"window_start": "2026-10-17T10:00:00+00:00", "device": "d1", "avg_temp": 22.0, "readings": 2},
        {"window_start": "2026-10-17T10:01:00+00:00", "device": "d2", "avg_temp": 19.0, "readings": 1},
    ]
