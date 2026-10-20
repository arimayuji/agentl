"""Client-side rate limits, Retry-After and budget.max_calls (#336). No real time passes."""

import email.utils
import io
import json
import threading
import urllib.error

import pytest

from agentl import SpecError, llm, parse, register_provider, stats
from agentl.llm import RateLimiter, Result
from agentl.runner import Runner, agent_cache_key
from agentl.store import Store


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.lock = threading.Lock()

    def __call__(self) -> float:
        with self.lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self.lock:
            self.sleeps.append(seconds)
            self.now += seconds


def test_rpm_bucket_waits_for_the_next_request():
    clock = FakeClock()
    limiter = RateLimiter(rpm=60, clock=clock, sleep=clock.sleep)
    assert [limiter.acquire() for _ in range(60)] == [0.0] * 60  # starts full
    assert limiter.acquire() == pytest.approx(1.0)  # one request per second after that
    clock.now += 10
    assert limiter.acquire() == 0.0


def test_tpm_bucket_and_reconcile():
    clock = FakeClock()
    limiter = RateLimiter(tpm=600, clock=clock, sleep=clock.sleep)
    assert limiter.acquire(600) == 0.0
    assert limiter.acquire(300) == pytest.approx(30.0)  # 10 tokens per second
    limiter.reconcile(300, 600)  # the call used twice the estimate: 300 tokens owed
    assert limiter.acquire(100) == pytest.approx(40.0)
    limiter.reconcile(100, 0 + 50)  # used less: the rest goes back
    assert limiter.acquire(50) == 0.0


def test_call_larger_than_the_bucket_waits_for_a_full_one():
    clock = FakeClock()
    limiter = RateLimiter(tpm=100, clock=clock, sleep=clock.sleep)
    limiter.acquire(100)
    assert limiter.acquire(10_000) == pytest.approx(60.0)


def test_bucket_is_shared_by_threads():
    clock = FakeClock()
    limiter = RateLimiter(rpm=10, clock=clock, sleep=clock.sleep)
    threads = [threading.Thread(target=limiter.acquire) for _ in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 10 at once, then 20 more at 6 seconds each: no thread got a request early
    assert clock.now >= 20 * 6 - 1e-6


def test_rate_limit_is_validated():
    base = {"name": "p", "steps": [{"id": "a", "fn": "tests.test_rate_limit:rows"}]}
    for bad in ({}, {"rpm": 0}, {"tpm": -1}, {"rpm": 1, "burst": 2}):
        with pytest.raises(SpecError):
            parse({**base, "llm": {"default": {"provider": "echo", "rate_limit": bad}}})
    spec = parse({**base, "llm": {"default": {"provider": "echo", "rate_limit": {"tpm": 1000}}}})
    assert spec.llm["default"].rate_limit is not None and spec.llm["default"].rate_limit.tpm == 1000


def test_rate_limit_does_not_change_cache_keys():
    def key(llm_spec):
        spec = parse(pipeline(llm_spec))
        return agent_cache_key(spec, spec.step("ask"), {"i": 1})

    assert key({"provider": "counted", "model": "m"}) == key(
        {"provider": "counted", "model": "m", "rate_limit": {"rpm": 5}}
    )


class Counted:
    calls = 0

    def __init__(self, spec):
        pass

    def structured(self, instruction, record, schema):
        Counted.calls += 1
        return Result(schema.model_validate({"ok": True}), 100, 20)

    def text(self, instruction, content):
        return ""


register_provider("counted", Counted)


def rows(input=None, n=5):
    return [{"i": i} for i in range(n)]


def pipeline(llm_spec, budget=None, n=5, concurrency=1):
    raw = {
        "name": "rl",
        "llm": {"default": llm_spec},
        "steps": [
            {"id": "src", "fn": "tests.test_rate_limit:rows", "with": {"n": n}},
            {
                "id": "ask",
                "agent": "Check.",
                "input": "src",
                "output": {"ok": "bool"},
                "concurrency": concurrency,
            },
        ],
    }
    if budget:
        raw["budget"] = budget
    return raw


@pytest.fixture
def fake_limiter(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(
        llm, "rate_limiter", lambda spec: RateLimiter(spec.rpm, spec.tpm, clock=clock, sleep=clock.sleep)
    )
    Counted.calls = 0
    return clock


@pytest.mark.parametrize("concurrency", [1, 4])
def test_run_waits_and_reports_throttling(tmp_path, fake_limiter, concurrency):
    spec = parse(
        pipeline({"provider": "counted", "model": "m", "rate_limit": {"rpm": 2}}, concurrency=concurrency)
    )
    events: list[dict] = []
    with Store(tmp_path / "db.sqlite") as store:
        result = Runner(spec, store=store, use_cache=False, listeners=[events.append]).run()
        report = stats.compute(store, since=0)
    assert result.status == "succeeded" and Counted.calls == 5
    throttled = [e for e in events if e["type"] == "llm.throttled"]
    assert len(throttled) == 3  # 2 at once, then one every 30 s
    assert all(e["step"] == "ask" and e["data"]["llm"] == "default" for e in throttled)
    assert result.throttled_s == pytest.approx(90.0)
    assert result.spend["ask"]["throttled_s"] == pytest.approx(90.0)
    done = [e for e in events if e["type"] == "run.succeeded"][-1]
    assert done["data"]["throttled_s"] == pytest.approx(90.0)
    assert report["pipelines"][0]["throttled_s"] == pytest.approx(90.0)


def test_no_rate_limit_no_event(tmp_path, fake_limiter):
    spec = parse(pipeline({"provider": "counted", "model": "m"}))
    events: list[dict] = []
    with Store(tmp_path / "db.sqlite") as store:
        result = Runner(spec, store=store, use_cache=False, listeners=[events.append]).run()
    assert result.throttled_s == 0 and "throttled_s" not in result.spend["ask"]
    assert not [e for e in events if e["type"] == "llm.throttled"]
    assert fake_limiter.sleeps == []


@pytest.mark.parametrize("concurrency", [1, 4])
def test_max_calls_stops_the_run(tmp_path, fake_limiter, concurrency):
    spec = parse(
        pipeline(
            {"provider": "counted", "model": "m"}, budget={"max_calls": 3}, n=10, concurrency=concurrency
        )
    )
    events: list[dict] = []
    with Store(tmp_path / "db.sqlite") as store:
        result = Runner(spec, store=store, use_cache=False, listeners=[events.append]).run()
    assert result.status == "failed" and "max_calls 3" in (result.error or "")
    assert Counted.calls == 3  # calls in flight are reserved, so parallel workers stop at 3 too
    assert [e for e in events if e["type"] == "budget.exceeded"]


def test_max_calls_is_validated():
    with pytest.raises(SpecError):
        parse(pipeline({"provider": "counted", "model": "m"}, budget={"max_calls": 0}))


# Retry-After in the OpenAI-compatible provider ------------------------------------------


def _provider(monkeypatch, replies):
    spec = parse(
        pipeline(
            {
                "provider": "openai-compatible",
                "model": "m",
                "options": {"base_url": "http://x/v1", "retries": 3},
            }
        )
    ).llm["default"]
    prov = llm.provider(spec)
    waits: list[float] = []
    prov.sleep = waits.append  # type: ignore[attr-defined]

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout, context=None):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Resp(json.dumps(reply).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return prov, waits


def _429(retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError("http://x", 429, "slow down", headers, io.BytesIO())  # type: ignore[arg-type]


ANSWER = {"choices": [{"message": {"content": "hi"}}], "usage": {}}


def test_retry_after_seconds_date_and_cap(monkeypatch):
    date = email.utils.formatdate(llm.time.time() + 30, usegmt=True)
    prov, waits = _provider(monkeypatch, [_429("2"), _429(date), _429("3600"), ANSWER])
    assert prov.text("Say hi.", "x") == "hi"
    assert waits[0] == 2.0
    assert 28 <= waits[1] <= 30
    assert waits[2] == llm.RETRY_AFTER_CAP


def test_no_retry_after_backs_off_exponentially(monkeypatch):
    prov, waits = _provider(monkeypatch, [_429(), _429("soon"), ANSWER])
    assert prov.text("Say hi.", "x") == "hi"
    assert waits == [1, 2]


def test_parse_retry_after():
    assert llm.parse_retry_after("1.5") == 1.5
    assert llm.parse_retry_after("-3") == 0.0
    assert llm.parse_retry_after(None) is None
    assert llm.parse_retry_after("later") is None
    assert llm.parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412470.0) == 10.0
