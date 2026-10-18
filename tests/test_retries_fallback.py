"""Step-level retries and fallbacks (#296)."""

import io
import urllib.error

import pytest

from agentl import lint, parse, runner
from agentl.registry import connector
from agentl.runner import Runner
from agentl.store import Store

CALLS: list[str] = []
FAILURES: dict[str, list[BaseException]] = {}


def _next_failure(name: str) -> None:
    CALLS.append(name)
    queue = FAILURES.get(name, [])
    if queue:
        raise queue.pop(0)


@connector("test.flaky")
def flaky(input=None, name="main", rows=2):
    _next_failure(name)
    return [{"id": i, "from": name} for i in range(rows)]


@connector("test.flaky.write")
def flaky_write(input=None, name="writer"):
    _next_failure(name)
    return [{"written": len(input or [])}]


def _http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("https://example.com", code, "err", headers, io.BytesIO())  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    CALLS.clear()
    FAILURES.clear()
    waits: list[float] = []
    monkeypatch.setattr(runner, "_sleep", waits.append)
    monkeypatch.setattr(runner, "_jitter", lambda: 1.0)
    return waits


def _spec(step: dict, extra=()):
    return parse(
        {
            "name": "prices",
            "steps": [{"id": "prices", "use": "test.flaky", **step}, *extra],
        }
    )


def _run(tmp_path, spec):
    with Store(tmp_path / "a.db") as store:
        result = Runner(spec, store=store, use_cache=False).run()
        return result, store.events(result.run_id)


def _types(events, kind):
    return [e["data"] for e in events if e["type"] == kind]


def test_retry_succeeds_on_third_attempt_with_backoff(tmp_path, setup):
    FAILURES["main"] = [TimeoutError("slow"), ConnectionError("reset")]
    spec = _spec({"retries": {"attempts": 3, "backoff": "2s", "max_backoff": "3s"}})
    result, events = _run(tmp_path, spec)
    assert result.status == "succeeded" and result.outcome == "success"
    assert CALLS == ["main", "main", "main"]
    assert setup == [2.0, 3.0]  # 2s, then 4s capped at 3s
    retries = _types(events, "step.retry")
    assert [r["attempt"] for r in retries] == [1, 2]
    assert [r["wait"] for r in retries] == [2.0, 3.0]
    assert "slow" in retries[0]["error"]


def test_retry_after_header_is_honored(tmp_path, setup):
    FAILURES["main"] = [_http_error(429, retry_after="7")]
    spec = _spec({"retries": {"attempts": 2, "backoff": "1s", "max_backoff": "30s", "on": ["http_429"]}})
    result, _ = _run(tmp_path, spec)
    assert result.status == "succeeded" and setup == [7.0]


def test_retry_stops_on_non_matching_error(tmp_path, setup):
    FAILURES["main"] = [_http_error(404), TimeoutError("never reached")]
    spec = _spec({"retries": {"attempts": 3, "on": ["transport", "http_5xx"]}})
    result, events = _run(tmp_path, spec)
    assert result.status == "failed" and CALLS == ["main"] and setup == []
    assert not _types(events, "step.retry")


def test_http_5xx_and_any(tmp_path):
    FAILURES["main"] = [_http_error(503), ValueError("bad")]
    spec = _spec({"retries": {"attempts": 3, "on": ["http_5xx"]}})
    assert _run(tmp_path, spec)[0].status == "failed" and CALLS == ["main", "main"]
    CALLS.clear()
    FAILURES["main"] = [_http_error(503), ValueError("bad")]
    spec = _spec({"retries": {"attempts": 3, "on": ["any"]}})
    assert _run(tmp_path, spec)[0].status == "succeeded" and len(CALLS) == 3


def test_no_retries_by_default(tmp_path, setup):
    FAILURES["main"] = [TimeoutError("slow")]
    result, _ = _run(tmp_path, _spec({}))
    assert result.status == "failed" and CALLS == ["main"] and setup == []


def test_attempts_exhausted_then_fallback_chain(tmp_path):
    FAILURES["main"] = [TimeoutError("a"), TimeoutError("b")]
    FAILURES["crawl"] = [RuntimeError("crawl broke")]
    spec = _spec(
        {
            "retries": {"attempts": 2},
            "fallback": [
                {"use": "test.flaky", "with": {"name": "crawl"}},
                {"use": "test.flaky", "with": {"name": "cache", "rows": 1}},
            ],
            "fallback_tag": True,
        }
    )
    result, events = _run(tmp_path, spec)
    assert result.status == "succeeded"
    assert CALLS == ["main", "main", "crawl", "cache"]
    assert result.outputs["prices"] == [{"id": 0, "from": "cache", "_source": "fallback[1]"}]
    assert result.fallbacks == {"prices": "fallback[1]"}
    assert result.outcome == "partial"
    fb = _types(events, "step.fallback")
    assert [(f["from"], f["to"]) for f in fb] == [("test.flaky", "test.flaky"), ("test.flaky", "test.flaky")]
    assert "b" in fb[0]["error"] and "crawl broke" in fb[1]["error"]


def test_main_success_is_tagged_main_and_success(tmp_path):
    spec = _spec({"fallback": [{"use": "test.flaky", "with": {"name": "alt"}}], "fallback_tag": True})
    result, events = _run(tmp_path, spec)
    assert result.outcome == "success" and not result.fallbacks
    assert all(r["_source"] == "main" for r in result.outputs["prices"])
    assert not _types(events, "step.fallback")


def test_fallback_outcome_success(tmp_path):
    FAILURES["main"] = [RuntimeError("down")]
    spec = _spec(
        {"fallback": [{"use": "test.flaky", "with": {"name": "alt"}}], "fallback_outcome": "success"}
    )
    result, _ = _run(tmp_path, spec)
    assert result.status == "succeeded" and result.outcome == "success"
    assert result.outputs["prices"][0]["from"] == "alt"


def test_every_fallback_fails(tmp_path):
    FAILURES["main"] = [RuntimeError("down")]
    FAILURES["alt"] = [RuntimeError("alt down")]
    spec = _spec({"fallback": [{"use": "test.flaky", "with": {"name": "alt"}}]})
    result, _ = _run(tmp_path, spec)
    assert result.status == "failed" and "alt down" in (result.error or "")


def test_downstream_reads_the_fallback_output(tmp_path):
    FAILURES["main"] = [RuntimeError("down")]
    spec = _spec(
        {"fallback": [{"use": "test.flaky", "with": {"name": "alt"}}]},
        extra=[{"id": "out", "use": "test.flaky.write", "input": "prices"}],
    )
    result, _ = _run(tmp_path, spec)
    assert result.outputs["out"] == [{"written": 2}]


def test_validation_errors():
    with pytest.raises(ValueError, match="own fallback"):
        _spec({"fallback": [{"use": "test.flaky", "fallback": [{"use": "test.flaky"}]}]})
    with pytest.raises(ValueError, match="fall back between llms"):
        parse(
            {
                "name": "p",
                "steps": [
                    {"id": "a", "agent": "x", "output": {"x": "str"}, "fallback": [{"use": "test.flaky"}]}
                ],
            }
        )
    with pytest.raises(ValueError, match="fallback_writer"):
        parse(
            {
                "name": "p",
                "steps": [{"id": "w", "use": "test.flaky.write", "fallback": [{"use": "csv.write"}]}],
            }
        )
    parse(
        {
            "name": "p",
            "steps": [
                {
                    "id": "w",
                    "use": "test.flaky.write",
                    "fallback": [{"use": "csv.write"}],
                    "fallback_writer": True,
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="need a fallback"):
        _spec({"fallback_tag": True})
    with pytest.raises(ValueError):
        _spec({"retries": {"attempts": 2, "on": ["sometimes"]}})
    with pytest.raises(ValueError, match="duration"):
        _spec({"retries": {"backoff": "soon"}})


def test_lint_unknown_fallback_connector_and_no_retries(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text(
        "name: p\nsteps:\n  - id: prices\n    use: http.get\n    with: {url: https://example.com}\n"
        "    fallback:\n      - use: nope.read\n"
    )
    codes = {f.code for f in lint.check_file(p).findings}
    assert "AE003" in codes and "AL011" in codes
