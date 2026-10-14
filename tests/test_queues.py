"""Queues (#25): micro-batch consumers acknowledge only after the run succeeds.

The broker clients are replaced by fakes that record what agentl asks of them; no broker runs
here. What is tested is agentl's side: batching, decoding, and when it commits, deletes, acks or
gives messages back."""

import json
import types
from typing import ClassVar

import pytest

from agentl import parse
from agentl.connectors import queues
from agentl.registry import get
from agentl.runner import Runner
from agentl.store import Store


# ---------------------------------------------------------------- fakes
class KMsg:
    def __init__(self, value, offset, key=b"k", error=None):
        self._v, self._o, self._k, self._e = value, offset, key, error

    def error(self):
        return self._e

    def value(self):
        return self._v

    def key(self):
        return self._k

    def topic(self):
        return "invoices"

    def partition(self):
        return 0

    def offset(self):
        return self._o

    def timestamp(self):
        return (1, 1760000000000)

    def headers(self):
        return [("source", b"erp")]


class FakeKafka:
    calls: ClassVar[list] = []
    pending: ClassVar[list] = []

    class KafkaError:
        _PARTITION_EOF = -191

    class Consumer:
        def __init__(self, conf):
            FakeKafka.calls.append(("conf", conf))

        def subscribe(self, topics):
            FakeKafka.calls.append(("subscribe", topics))

        def consume(self, num_messages, timeout):
            batch, FakeKafka.pending[:] = FakeKafka.pending[:num_messages], FakeKafka.pending[num_messages:]
            return batch

        def commit(self, asynchronous):
            FakeKafka.calls.append(("commit", asynchronous))

        def close(self):
            FakeKafka.calls.append(("close",))

    class Producer:
        sent: ClassVar[list] = []

        def __init__(self, conf):
            self.cb = []

        def produce(self, topic, value, key, on_delivery):
            FakeKafka.Producer.sent.append((topic, key, json.loads(value)))
            self.cb.append(on_delivery)

        def poll(self, t):
            pass

        def flush(self, timeout):
            for cb in self.cb:
                cb(None, None)
            return 0


class FakeSQS:
    def __init__(self, messages):
        self.messages, self.deleted, self.released, self.sent = list(messages), [], [], []

    def receive_message(self, QueueUrl, MaxNumberOfMessages, **kw):
        batch, self.messages = self.messages[:MaxNumberOfMessages], self.messages[MaxNumberOfMessages:]
        return {"Messages": batch}

    def delete_message_batch(self, QueueUrl, Entries):
        self.deleted += [e["ReceiptHandle"] for e in Entries]
        return {"Successful": Entries}

    def change_message_visibility_batch(self, QueueUrl, Entries):
        self.released += [(e["ReceiptHandle"], e["VisibilityTimeout"]) for e in Entries]

    def send_message_batch(self, QueueUrl, Entries):
        self.sent.append(Entries)
        return {"Successful": Entries}


class FakePika:
    log: ClassVar[list] = []
    queue: ClassVar[list] = []

    class URLParameters:
        def __init__(self, url):
            pass

    class BasicProperties:
        def __init__(self, **kw):
            self.kw = kw

    class BlockingConnection:
        def __init__(self, params):
            pass

        def channel(self):
            return FakePika.Channel()

        def close(self):
            FakePika.log.append(("close",))

    class Channel:
        def basic_get(self, queue, auto_ack):
            if not FakePika.queue:
                return None, None, None
            tag, body = FakePika.queue.pop(0)
            return types.SimpleNamespace(delivery_tag=tag, routing_key="orders.new"), None, body

        def basic_ack(self, delivery_tag, multiple):
            FakePika.log.append(("ack", delivery_tag, multiple))

        def basic_nack(self, delivery_tag, multiple, requeue):
            FakePika.log.append(("nack", delivery_tag, multiple, requeue))

        def confirm_delivery(self):
            pass

        def basic_publish(self, exchange, routing_key, body, properties, mandatory):
            FakePika.log.append(("publish", routing_key, json.loads(body)))


class FakePubSub:
    log: ClassVar[list] = []

    class SubscriberClient:
        def __init__(self):
            self.served = False

        def pull(self, request, timeout):
            if self.served:
                return types.SimpleNamespace(received_messages=[])
            self.served = True
            msgs = [
                types.SimpleNamespace(
                    ack_id=f"a{i}",
                    message=types.SimpleNamespace(
                        data=json.dumps({"n": i}).encode(),
                        attributes={"k": "v"},
                        message_id=str(i),
                        publish_time="t",
                    ),
                )
                for i in range(3)
            ]
            return types.SimpleNamespace(received_messages=msgs)

        def acknowledge(self, request):
            FakePubSub.log.append(("ack", request["ack_ids"]))

        def modify_ack_deadline(self, request):
            FakePubSub.log.append(("nack", request["ack_ids"], request["ack_deadline_seconds"]))


@pytest.fixture
def fakes(monkeypatch):
    FakeKafka.calls.clear()
    FakeKafka.Producer.sent.clear()
    FakeKafka.pending[:] = [KMsg(json.dumps({"id": i}).encode(), i) for i in range(5)]
    FakePika.log.clear()
    FakePika.queue[:] = [(1, b'{"id": 1}'), (2, b"plain text")]
    FakePubSub.log.clear()
    sqs = FakeSQS(
        [{"Body": json.dumps({"id": i}), "MessageId": f"m{i}", "ReceiptHandle": f"h{i}"} for i in range(13)]
    )
    mods = {
        "confluent_kafka": FakeKafka,
        "boto3": types.SimpleNamespace(client=lambda name, **kw: sqs),
        "pika": FakePika,
        "google.cloud.pubsub_v1": FakePubSub,
    }
    monkeypatch.setattr(queues, "require", lambda module, extra: mods[module])
    return sqs


# ---------------------------------------------------------------- connectors called directly
def test_kafka_batch_limit_and_decoding(fakes):
    rows = get("kafka.consume")(None, brokers="b:9092", topic="invoices", max_records=3, max_seconds=1)
    assert [r["value"] for r in rows] == [{"id": 0}, {"id": 1}, {"id": 2}]
    assert rows[0]["key"] == "k" and rows[0]["headers"] == {"source": "erp"} and rows[0]["offset"] == 0
    conf = FakeKafka.calls[0][1]
    assert conf["enable.auto.commit"] is False and conf["group.id"] == "agentl"
    assert ("commit", False) in FakeKafka.calls  # called directly: committed at once


def test_kafka_produce(fakes):
    out = get("kafka.produce")([{"id": 1, "v": 2}], brokers="b", topic="out", key="id")
    assert out == [{"topic": "out", "produced": 1}] and FakeKafka.Producer.sent == [
        ("out", b"1", {"id": 1, "v": 2})
    ]


def test_sqs_receive_send(fakes):
    rows = get("sqs.receive")(None, queue_url="q", max_records=12, max_seconds=1)
    assert len(rows) == 12 and rows[0] == {"body": {"id": 0}, "message_id": "m0", "attributes": {}}
    assert fakes.deleted == [f"h{i}" for i in range(12)]  # deleted in batches of 10
    get("sqs.send")([{"id": i} for i in range(12)], queue_url="q", group_id="g-{id}", dedup_field="id")
    assert [len(b) for b in fakes.sent] == [10, 2] and fakes.sent[0][3]["MessageGroupId"] == "g-3"


def test_rabbitmq_and_pubsub_formats(fakes):
    rows = get("rabbitmq.consume")(None, url="amqp://x", queue="orders", max_seconds=0.3)
    assert [r["body"] for r in rows] == [{"id": 1}, "plain text"]  # auto: JSON when it parses
    assert ("ack", 2, True) in FakePika.log
    rows = get("pubsub.pull")(None, subscription="projects/p/subscriptions/s", max_seconds=1)
    assert [r["data"] for r in rows] == [{"n": 0}, {"n": 1}, {"n": 2}] and FakePubSub.log == [
        ("ack", ["a0", "a1", "a2"])
    ]
    with pytest.raises(ValueError, match="format must be"):
        get("rabbitmq.consume")(None, url="amqp://x", queue="q", format="xml")


# ---------------------------------------------------------------- inside a run: ack only on success
SAVED: list = []


def load(input):
    payloads = [r.get("value") or r.get("body") or r.get("data") for r in input]
    if any(isinstance(p, dict) and p.get("id") == 99 for p in payloads):
        raise RuntimeError("warehouse rejected the batch")
    SAVED.extend(input)
    return [{"written": len(input)}]


def pipeline(use, args):
    return parse(
        {
            "name": "stream",
            "steps": [
                {"id": "read", "use": use, "with": args},
                {"id": "load", "fn": "tests.test_queues:load", "input": "read"},
            ],
        }
    )


def run(tmp_path, spec):
    SAVED.clear()
    with Store(tmp_path / "s.db") as store:
        return Runner(spec, store=store).run()


def test_kafka_commits_only_after_a_successful_run(fakes, tmp_path):
    r = run(tmp_path, pipeline("kafka.consume", {"brokers": "b", "topic": "invoices", "max_seconds": 0.2}))
    assert r.status == "succeeded" and len(SAVED) == 5
    assert FakeKafka.calls[-2:] == [("commit", False), ("close",)]

    FakeKafka.calls.clear()
    FakeKafka.pending[:] = [KMsg(b'{"id": 99}', 7)]
    r = run(tmp_path, pipeline("kafka.consume", {"brokers": "b", "topic": "invoices", "max_seconds": 0.2}))
    assert (
        r.status == "failed"
        and ("commit", False) not in FakeKafka.calls
        and FakeKafka.calls[-1] == ("close",)
    )


def test_sqs_failed_run_releases_and_keeps_messages(fakes, tmp_path):
    fakes.messages = [{"Body": '{"id": 99}', "MessageId": "x", "ReceiptHandle": "hx"}]
    r = run(
        tmp_path, pipeline("sqs.receive", {"queue_url": "q", "max_seconds": 1, "release_on_failure": True})
    )
    assert r.status == "failed" and fakes.deleted == [] and fakes.released == [("hx", 0)]


def test_rabbitmq_requeues_on_failure(fakes, tmp_path):
    FakePika.queue[:] = [(5, b'{"id": 99}')]
    r = run(tmp_path, pipeline("rabbitmq.consume", {"url": "amqp://x", "queue": "q", "max_seconds": 0.3}))
    assert (
        r.status == "failed"
        and ("nack", 5, True, True) in FakePika.log
        and not any(e[0] == "ack" for e in FakePika.log)
    )


def test_failed_ack_fails_the_run(fakes, tmp_path, monkeypatch):
    def broken(self, QueueUrl, Entries):
        return {"Failed": [{"Id": "0", "Code": "ReceiptHandleIsInvalid"}]}

    monkeypatch.setattr(FakeSQS, "delete_message_batch", broken)
    with Store(tmp_path / "s.db") as store:
        r = Runner(pipeline("sqs.receive", {"queue_url": "q", "max_seconds": 1}), store=store).run()
        events = [e["type"] for e in store.events(r.run_id)]
    assert r.status == "failed" and "acknowledging the messages read failed" in r.error
    assert "ack.failed" in events and events[-1] == "run.failed"


def test_every_trigger_spec():
    spec = parse({"name": "x", "trigger": [{"every": "30s"}], "steps": [{"id": "a", "use": "trigger"}]})
    assert spec.trigger[0].kind == "every"


def test_every_trigger_runs_back_to_back_without_piling_up(tmp_path):
    import threading

    from agentl.triggers import Service

    gate, calls = threading.Event(), []

    def slow(spec_, records, kind):
        calls.append(kind)
        gate.wait(5)

    spec = parse({"name": "x", "trigger": [{"every": "10s"}], "steps": [{"id": "a", "use": "trigger"}]})
    with Store(tmp_path / "s.db") as store:
        svc = Service([spec], store, run=slow)
        svc.tick(now=0)
        svc.tick(now=20)  # first run still going: nothing queued
        gate.set()
        svc.queues["x"].join()
        svc.tick(now=21)
        svc.queues["x"].join()
    assert calls == ["every", "every"]
