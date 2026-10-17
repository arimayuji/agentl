"""Kinesis and Event Hubs readers (#226): checkpoints in the run store, moved only on success.

boto3 and azure-eventhub are replaced by fakes; nothing here touches the network."""

import datetime as dt
import threading
import types
from typing import ClassVar

import pytest

from agentl import parse
from agentl.connectors import queues
from agentl.registry import get
from agentl.runner import Runner
from agentl.store import Store

T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
SAVED: list = []


def load(rows):
    SAVED.extend(rows)
    if any(isinstance(r["value"], dict) and r["value"].get("t") == 99 for r in rows):
        raise RuntimeError("load failed")
    return rows


def run(tmp_path, use, args):
    SAVED.clear()
    spec = parse(
        {
            "name": "devices",
            "steps": [
                {"id": "read", "use": use, "with": args},
                {"id": "load", "fn": "tests.test_streams:load", "input": "read"},
            ],
        }
    )
    with Store(tmp_path / "s.db") as store:
        r = Runner(spec, store=store).run()
        return r, store.state_get("devices", "read")


# ---------------------------------------------------------------- Kinesis
class FakeKinesis:
    """Shards: closed parent `s0` (two records), its child `s1`, open shard `s2`."""

    def __init__(self):
        self.data = {
            "s0": [(b'{"t": 1}', "1"), (b"plain", "2")],
            "s1": [(b'{"t": 3}', "3")],
            "s2": [(b'{"t": 4}', "4"), (b'{"t": 5}', "5")],
        }
        self.closed = {"s0"}
        self.calls: list = []

    def list_shards(self, **kw):
        self.calls.append(("list", kw))
        if "NextToken" not in kw:
            return {
                "Shards": [{"ShardId": "s0"}, {"ShardId": "s1", "ParentShardId": "s0"}],
                "NextToken": "p2",
            }
        return {"Shards": [{"ShardId": "s2"}]}

    def get_shard_iterator(self, StreamName=None, StreamARN=None, ShardId="", **kw):
        self.calls.append(("iter", ShardId, kw))
        seqs = [s for _, s in self.data[ShardId]]
        if kw["ShardIteratorType"] == "AFTER_SEQUENCE_NUMBER":
            pos = seqs.index(kw["StartingSequenceNumber"]) + 1
        elif kw["ShardIteratorType"] == "LATEST":
            pos = len(seqs)
        else:
            pos = 0
        return {"ShardIterator": f"{ShardId}:{pos}"}

    def get_records(self, ShardIterator, Limit):
        sid, pos = ShardIterator.split(":")
        rows = self.data[sid][int(pos) : int(pos) + Limit]
        end = int(pos) + len(rows)
        nxt = None if sid in self.closed and end >= len(self.data[sid]) else f"{sid}:{end}"
        recs = [
            {"Data": d, "PartitionKey": "dev-1", "SequenceNumber": s, "ApproximateArrivalTimestamp": T0}
            for d, s in rows
        ]
        return {"Records": recs, "NextShardIterator": nxt, "MillisBehindLatest": 0}


@pytest.fixture
def kinesis(monkeypatch):
    fake = FakeKinesis()
    boto3 = types.SimpleNamespace(client=lambda name, **kw: fake)
    monkeypatch.setattr(queues, "require", lambda mod, extra: boto3)
    return fake


def test_kinesis_records_order_and_limits(kinesis):
    rows = get("kinesis.read")(None, stream="sensor-stream", max_records=3)
    assert [r["sequence_number"] for r in rows] == ["1", "2", "3"]  # parent before child, then cut
    assert rows[0] == {
        "value": {"t": 1},
        "partition_key": "dev-1",
        "sequence_number": "1",
        "shard_id": "s0",
        "arrival_timestamp": "2026-01-01T00:00:00+00:00",
    }
    assert rows[1]["value"] == "plain"
    assert kinesis.calls[0] == ("list", {"StreamName": "sensor-stream"})
    with pytest.raises(ValueError, match="start"):
        get("kinesis.read")(None, stream="x", start="AT_TIMESTAMP")
    with pytest.raises(ValueError):
        get("kinesis.read")(None, stream="x", format="xml")
    with pytest.raises(ValueError):  # JSONDecodeError
        get("kinesis.read")(None, stream="arn:aws:kinesis:r:1:stream/x", format="json")  # "plain"


def test_kinesis_latest_and_arn(kinesis):
    rows = get("kinesis.read")(None, stream="arn:aws:kinesis:r:1:stream/x", start="LATEST")
    # s0 and s2 start at LATEST; s1, the child of the closed s0, from its own start
    assert [r["sequence_number"] for r in rows] == ["3"]
    assert kinesis.calls[0] == ("list", {"StreamARN": "arn:aws:kinesis:r:1:stream/x"})


def test_kinesis_checkpoint_only_on_success(kinesis, tmp_path):
    r, state = run(tmp_path, "kinesis.read", {"stream": "s", "max_records": 2})
    assert r.status == "succeeded" and len(SAVED) == 2
    assert state == {"shards": {"s0": "2"}, "closed": ["s0"]}

    kinesis.data["s1"] = [(b'{"t": 99}', "3")]  # the load fails on this one
    r, state = run(tmp_path, "kinesis.read", {"stream": "s"})
    assert r.status == "failed" and state == {"shards": {"s0": "2"}, "closed": ["s0"]}

    kinesis.data["s1"] = [(b'{"t": 3}', "3")]
    r, state = run(tmp_path, "kinesis.read", {"stream": "s"})
    assert r.status == "succeeded" and [x["sequence_number"] for x in SAVED] == ["3", "4", "5"]
    assert state["shards"] == {"s0": "2", "s1": "3", "s2": "5"}
    assert ("iter", "s0", {}) not in kinesis.calls

    kinesis.data["s2"].append((b'{"t": 6}', "6"))
    r, state = run(tmp_path, "kinesis.read", {"stream": "s"})
    assert [x["sequence_number"] for x in SAVED] == ["6"]  # resumes after the checkpoint


# ---------------------------------------------------------------- Event Hubs
class Ev:
    def __init__(self, body, seq):
        self.body, self.sequence_number, self.offset = body, seq, str(seq * 100)
        self.enqueued_time, self.partition_key, self.properties = T0, "dev-1", {b"kind": b"temp"}

    def body_as_str(self, encoding="utf-8"):
        return self.body


class FakeHub:
    events: ClassVar[dict] = {}
    log: ClassVar[list] = []

    def __init__(self, **kw):
        FakeHub.log.append(("init", kw))
        self.stop = threading.Event()

    @classmethod
    def from_connection_string(cls, conn, **kw):
        return cls(conn=conn, **kw)

    def get_partition_ids(self):
        return ["0", "1"]

    def receive_batch(self, on_event_batch, on_error, starting_position, starting_position_inclusive, **kw):
        FakeHub.log.append(("receive", dict(starting_position), starting_position_inclusive))
        for pid in ("0", "1"):
            pos = starting_position[pid]
            evs = [] if pos == "@latest" else FakeHub.events.get(pid, [])
            evs = [e for e in evs if pos == "-1" or e.sequence_number > pos]
            if evs:
                on_event_batch(types.SimpleNamespace(partition_id=pid), evs)
        self.stop.wait()

    def close(self):
        FakeHub.log.append(("close",))
        self.stop.set()


@pytest.fixture
def hub(monkeypatch):
    FakeHub.log = []
    FakeHub.events = {"0": [Ev('{"t": 1}', 1), Ev("raw", 2)], "1": [Ev('{"t": 3}', 7)]}
    mod = types.SimpleNamespace(EventHubConsumerClient=FakeHub)
    monkeypatch.setattr(queues, "require", lambda m, extra: mod)
    return FakeHub


def test_eventhubs_records_and_limits(hub):
    rows = get("eventhubs.consume")(
        None, connection_string="Endpoint=sb://x/", eventhub="telemetry", max_seconds=5
    )
    assert [r["value"] for r in rows] == [{"t": 1}, "raw", {"t": 3}]
    assert rows[0] == {
        "value": {"t": 1},
        "partition_id": "0",
        "sequence_number": 1,
        "offset": "100",
        "enqueued_time": "2026-01-01T00:00:00+00:00",
        "partition_key": "dev-1",
        "properties": {"kind": "temp"},
    }
    init = hub.log[0][1]
    assert init["consumer_group"] == "$Default" and init["eventhub_name"] == "telemetry"
    assert hub.log[1] == ("receive", {"0": "-1", "1": "-1"}, False)

    rows = get("eventhubs.consume")(None, connection_string="c", max_records=2, max_seconds=5)
    assert len(rows) == 2 and ("close",) in hub.log
    rows = get("eventhubs.consume")(None, connection_string="c", start="latest", max_seconds=0.1)
    assert rows == []
    rows = get("eventhubs.consume")(None, connection_string="c", format="text", max_seconds=0.1)
    assert rows[0]["value"] == '{"t": 1}'
    with pytest.raises(ValueError, match="connection_string"):
        get("eventhubs.consume")(None, eventhub="x")
    with pytest.raises(ValueError, match="start"):
        get("eventhubs.consume")(None, connection_string="c", start="oldest")


def test_eventhubs_checkpoint_only_on_success(hub, tmp_path):
    args = {"connection_string": "c", "max_seconds": 0.2, "max_records": 2}
    r, state = run(tmp_path, "eventhubs.consume", args)
    assert r.status == "succeeded" and state == {"partitions": {"0": 2}}

    hub.events["1"] = [Ev('{"t": 99}', 7)]
    r, state = run(tmp_path, "eventhubs.consume", args)
    assert r.status == "failed" and state == {"partitions": {"0": 2}}

    hub.events["1"] = [Ev('{"t": 3}', 7)]
    hub.events["0"].append(Ev('{"t": 4}', 3))
    r, state = run(tmp_path, "eventhubs.consume", args)
    assert hub.log[-2] == ("receive", {"0": 2, "1": "-1"}, False)  # resumes after the checkpoint
    assert [x["sequence_number"] for x in SAVED] == [3, 7]
    assert r.status == "succeeded" and state == {"partitions": {"0": 3, "1": 7}}
