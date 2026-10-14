"""Queues and streams: Kafka, Amazon SQS, RabbitMQ and Google Pub/Sub.

Consumers read a micro-batch (up to `max_records`, or until `max_seconds` pass) and acknowledge
it only after the whole run succeeded: offsets are committed, messages deleted or acked. If the
run fails, nothing is acknowledged and the broker delivers the messages again (at-least-once), so
make the load idempotent (upsert by a key). Run them on a schedule or back to back with
`agentl serve` (`trigger: [{every: 30s}]`).

Install the client you need: `agentl-etl[kafka]`, `[aws]` (SQS), `[rabbitmq]` or `[pubsub]`."""

from __future__ import annotations

import json
import time
from typing import Any

from ..registry import after_run, connector, require
from ._util import single


def _decode(data: bytes | str | None, fmt: str) -> Any:
    if data is None:
        return None
    text = data.decode("utf-8", errors="replace") if isinstance(data, (bytes, bytearray)) else data
    if fmt == "text":
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if fmt == "json":
            raise
        return text  # auto: JSON when it parses, text otherwise


def _encode(rec: dict, field: str | None) -> bytes:
    value = rec.get(field) if field else rec
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str) and field:
        return value.encode()
    return json.dumps(value, ensure_ascii=False, default=str).encode()


def _check(fmt: str) -> None:
    if fmt not in ("auto", "json", "text"):
        raise ValueError("format must be auto, json or text")


# ---------------------------------------------------------------- Kafka
@connector("kafka.consume")
def kafka_consume(
    input: Any = None,
    brokers: str = "",
    topic: str | list[str] = "",
    group: str = "agentl",
    max_records: int = 5000,
    max_seconds: float = 10,
    offset_reset: str = "earliest",
    format: str = "auto",
    config: dict | None = None,
) -> list[dict]:
    """Up to `max_records` messages (or what arrives in `max_seconds`). Offsets are committed only
    when the run succeeds. Records: `value` (JSON parsed when it is JSON), `key`, `topic`,
    `partition`, `offset`, `timestamp`, `headers`."""
    _check(format)
    ck = require("confluent_kafka", "kafka")
    consumer = ck.Consumer(
        {
            "bootstrap.servers": brokers,
            "group.id": group,
            "enable.auto.commit": False,
            "auto.offset.reset": offset_reset,
            **(config or {}),
        }
    )
    consumer.subscribe([topic] if isinstance(topic, str) else list(topic))
    out: list[dict] = []
    deadline = time.monotonic() + max_seconds
    try:
        while len(out) < max_records and (left := deadline - time.monotonic()) > 0:
            for msg in consumer.consume(
                num_messages=min(500, max_records - len(out)), timeout=min(left, 1.0)
            ):
                err = msg.error()
                if err is not None:
                    if err.code() == ck.KafkaError._PARTITION_EOF:
                        continue
                    raise RuntimeError(f"kafka: {err}")
                out.append(
                    {
                        "value": _decode(msg.value(), format),
                        "key": _decode(msg.key(), "text"),
                        "topic": msg.topic(),
                        "partition": msg.partition(),
                        "offset": msg.offset(),
                        "timestamp": msg.timestamp()[1],
                        "headers": {k: _decode(v, "text") for k, v in (msg.headers() or [])},
                    }
                )
    except Exception:
        consumer.close()
        raise

    def commit() -> None:
        try:
            if out:
                consumer.commit(asynchronous=False)
        finally:
            consumer.close()

    after_run(commit, consumer.close)
    return out


@connector("kafka.produce")
def kafka_produce(
    input: Any,
    brokers: str = "",
    topic: str = "",
    key: str | None = None,
    value: str | None = None,
    config: dict | None = None,
    timeout: float = 30,
) -> list[dict]:
    """Publish each record (or its `value` field) as JSON, keyed by the `key` field. Waits for
    every delivery and fails if any is not confirmed."""
    ck = require("confluent_kafka", "kafka")
    producer = ck.Producer({"bootstrap.servers": brokers, **(config or {})})
    errors: list[str] = []

    def report(err: Any, msg: Any) -> None:
        if err is not None:
            errors.append(str(err))

    rows = single(input)
    for r in rows:
        k = None if key is None or r.get(key) is None else str(r[key]).encode()
        producer.produce(topic, value=_encode(r, value), key=k, on_delivery=report)
        producer.poll(0)
    left = producer.flush(timeout)
    if left or errors:
        raise RuntimeError(f"kafka: {left} message(s) not delivered; errors: {errors[:3]}")
    return [{"topic": topic, "produced": len(rows)}]


# ---------------------------------------------------------------- SQS
def _sqs(region: str | None, endpoint_url: str | None) -> Any:
    boto3 = require("boto3", "aws")
    return boto3.client("sqs", region_name=region, endpoint_url=endpoint_url)


@connector("sqs.receive")
def sqs_receive(
    input: Any = None,
    queue_url: str = "",
    max_records: int = 1000,
    max_seconds: float = 20,
    format: str = "auto",
    release_on_failure: bool = False,
    region: str | None = None,
    endpoint_url: str | None = None,
) -> list[dict]:
    """Receive up to `max_records` messages (long polling, at most `max_seconds`). They are deleted
    when the run succeeds; on failure they come back after their visibility timeout, or at once
    with `release_on_failure: true`. Records: `body`, `message_id`, `attributes`."""
    _check(format)
    sqs = _sqs(region, endpoint_url)
    out: list[dict] = []
    handles: list[str] = []
    deadline = time.monotonic() + max_seconds
    while len(out) < max_records and (left := deadline - time.monotonic()) > 0:
        res = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=min(10, max_records - len(out)),
            WaitTimeSeconds=int(min(20, max(0, left))),
            AttributeNames=["All"],
            MessageAttributeNames=["All"],
        )
        msgs = res.get("Messages", [])
        if not msgs:
            break  # long poll came back empty: the queue is drained for now
        for m in msgs:
            handles.append(m["ReceiptHandle"])
            attrs = {k: v.get("StringValue") for k, v in (m.get("MessageAttributes") or {}).items()}
            out.append(
                {"body": _decode(m["Body"], format), "message_id": m["MessageId"], "attributes": attrs}
            )

    def batches() -> list[list[dict]]:
        entries = [{"Id": str(i), "ReceiptHandle": h} for i, h in enumerate(handles)]
        return [entries[i : i + 10] for i in range(0, len(entries), 10)]

    def delete() -> None:
        for batch in batches():
            res = sqs.delete_message_batch(QueueUrl=queue_url, Entries=batch)
            if res.get("Failed"):
                raise RuntimeError(
                    f"sqs: could not delete {len(res['Failed'])} message(s): {res['Failed'][:2]}"
                )

    def release() -> None:
        if release_on_failure:
            for batch in batches():
                sqs.change_message_visibility_batch(
                    QueueUrl=queue_url, Entries=[{**e, "VisibilityTimeout": 0} for e in batch]
                )

    if handles:
        after_run(delete, release)
    return out


@connector("sqs.send")
def sqs_send(
    input: Any,
    queue_url: str = "",
    body: str | None = None,
    group_id: str | None = None,
    dedup_field: str | None = None,
    region: str | None = None,
    endpoint_url: str | None = None,
) -> list[dict]:
    """Send each record (or its `body` field) as JSON, 10 per request. For FIFO queues, `group_id`
    is a fixed group or `{field}`, and `dedup_field` names the field used as deduplication id."""
    sqs = _sqs(region, endpoint_url)
    rows = single(input)
    sent = 0
    for start in range(0, len(rows), 10):
        entries = []
        for i, r in enumerate(rows[start : start + 10]):
            e: dict[str, Any] = {"Id": str(i), "MessageBody": _encode(r, body).decode()}
            if group_id:
                e["MessageGroupId"] = group_id.format(**r)
            if dedup_field:
                e["MessageDeduplicationId"] = str(r[dedup_field])
            entries.append(e)
        res = sqs.send_message_batch(QueueUrl=queue_url, Entries=entries)
        if res.get("Failed"):
            raise RuntimeError(f"sqs: {len(res['Failed'])} message(s) not sent: {res['Failed'][:2]}")
        sent += len(entries)
    return [{"queue_url": queue_url, "sent": sent}]


# ---------------------------------------------------------------- RabbitMQ
@connector("rabbitmq.consume")
def rabbitmq_consume(
    input: Any = None,
    url: str = "",
    queue: str = "",
    max_records: int = 1000,
    max_seconds: float = 5,
    format: str = "auto",
) -> list[dict]:
    """Up to `max_records` messages from `queue` (or what arrives in `max_seconds`). Acked when the
    run succeeds, rejected back to the queue (requeue) when it fails. Records: `body`,
    `routing_key`, `headers`."""
    _check(format)
    pika = require("pika", "rabbitmq")
    conn = pika.BlockingConnection(pika.URLParameters(url))
    ch = conn.channel()
    out: list[dict] = []
    last_tag = None
    deadline = time.monotonic() + max_seconds
    try:
        while len(out) < max_records and time.monotonic() < deadline:
            method, props, body = ch.basic_get(queue=queue, auto_ack=False)
            if method is None:
                time.sleep(0.2)
                continue
            last_tag = method.delivery_tag
            out.append(
                {
                    "body": _decode(body, format),
                    "routing_key": method.routing_key,
                    "headers": dict(getattr(props, "headers", None) or {}),
                }
            )
    except Exception:
        conn.close()
        raise

    def ack() -> None:
        try:
            if last_tag is not None:
                ch.basic_ack(delivery_tag=last_tag, multiple=True)
        finally:
            conn.close()

    def requeue() -> None:
        try:
            if last_tag is not None:
                ch.basic_nack(delivery_tag=last_tag, multiple=True, requeue=True)
        finally:
            conn.close()

    after_run(ack, requeue)
    return out


@connector("rabbitmq.publish")
def rabbitmq_publish(
    input: Any,
    url: str = "",
    exchange: str = "",
    routing_key: str = "",
    body: str | None = None,
) -> list[dict]:
    """Publish each record (or its `body` field) as persistent JSON, with publisher confirms.
    `routing_key` takes `{field}` placeholders."""
    pika = require("pika", "rabbitmq")
    conn = pika.BlockingConnection(pika.URLParameters(url))
    try:
        ch = conn.channel()
        ch.confirm_delivery()
        rows = single(input)
        props = pika.BasicProperties(content_type="application/json", delivery_mode=2)
        for r in rows:
            ch.basic_publish(
                exchange=exchange,
                routing_key=routing_key.format(**r),
                body=_encode(r, body),
                properties=props,
                mandatory=True,
            )
    finally:
        conn.close()
    return [{"exchange": exchange, "published": len(rows)}]


# ---------------------------------------------------------------- Pub/Sub
@connector("pubsub.pull")
def pubsub_pull(
    input: Any = None,
    subscription: str = "",
    max_records: int = 1000,
    max_seconds: float = 10,
    format: str = "auto",
) -> list[dict]:
    """Pull up to `max_records` messages from a subscription (`projects/p/subscriptions/s`). Acked
    when the run succeeds; on failure their ack deadline is reset so they come back at once.
    Records: `data`, `attributes`, `message_id`, `publish_time`."""
    _check(format)
    pubsub = require("google.cloud.pubsub_v1", "pubsub")
    client = pubsub.SubscriberClient()
    out: list[dict] = []
    ack_ids: list[str] = []
    deadline = time.monotonic() + max_seconds
    while len(out) < max_records and (left := deadline - time.monotonic()) > 0:
        res = client.pull(
            request={"subscription": subscription, "max_messages": min(1000, max_records - len(out))},
            timeout=max(1.0, left),
        )
        if not res.received_messages:
            break
        for rm in res.received_messages:
            m = rm.message
            ack_ids.append(rm.ack_id)
            out.append(
                {
                    "data": _decode(m.data, format),
                    "attributes": dict(m.attributes),
                    "message_id": m.message_id,
                    "publish_time": str(m.publish_time),
                }
            )

    def chunks() -> list[list[str]]:
        return [ack_ids[i : i + 1000] for i in range(0, len(ack_ids), 1000)]

    def ack() -> None:
        for ids in chunks():
            client.acknowledge(request={"subscription": subscription, "ack_ids": ids})

    def nack() -> None:
        for ids in chunks():
            client.modify_ack_deadline(
                request={"subscription": subscription, "ack_ids": ids, "ack_deadline_seconds": 0}
            )

    if ack_ids:
        after_run(ack, nack)
    return out


@connector("pubsub.publish")
def pubsub_publish(input: Any, topic: str = "", data: str | None = None, timeout: float = 60) -> list[dict]:
    """Publish each record (or its `data` field) as JSON to `projects/p/topics/t`, waiting for
    every message id."""
    pubsub = require("google.cloud.pubsub_v1", "pubsub")
    client = pubsub.PublisherClient()
    rows = single(input)
    futures = [client.publish(topic, _encode(r, data)) for r in rows]
    ids = [f.result(timeout=timeout) for f in futures]
    return [{"topic": topic, "published": len(ids)}]
