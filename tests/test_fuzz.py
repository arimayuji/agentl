"""Property tests on the parsers that take untrusted input: whatever comes in, they either work
or raise their own documented error, never something unexpected."""

import contextlib
import urllib.error
from unittest import mock

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from agentl import netguard, parse  # noqa: E402
from agentl.spec import SpecError  # noqa: E402
from agentl.triggers import Cron, authorized, body_records  # noqa: E402
from agentl.waits import holds, parse_condition  # noqa: E402

FAST = settings(max_examples=200, deadline=None)

json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=20),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=4),
    max_leaves=20,
)
records = st.lists(st.dictionaries(st.sampled_from(["date", "n"]), json_values), max_size=5)


@FAST
@given(st.dictionaries(st.sampled_from(["name", "steps", "llm", "budget", "trigger", "x"]), json_values))
def test_spec_parse_only_raises_spec_errors(raw):
    with contextlib.suppress(SpecError):
        parse(raw, env={})


@FAST
@given(st.text(max_size=60))
def test_cron_only_raises_value_error(expr):
    with contextlib.suppress(ValueError):
        Cron(expr)


@FAST
@given(st.text(max_size=40), records)
def test_wait_conditions_only_raise_value_or_type_errors(cond, rows):
    try:
        parse_condition(cond)
    except ValueError:
        return
    with contextlib.suppress(ValueError, TypeError):  # e.g. comparing a date with a number
        holds(cond, rows)


@FAST
@given(st.binary(max_size=200))
def test_webhook_bodies_become_records_or_a_decode_error(body):
    try:
        rows = body_records(body)
    except (ValueError, UnicodeDecodeError):
        return
    assert all(isinstance(r, dict) for r in rows)


@FAST
@given(st.text(min_size=1, max_size=30), st.binary(max_size=50), st.text(max_size=80))
def test_webhook_auth_only_accepts_the_secret(secret, body, header):
    assert not authorized(secret, {"Authorization": f"Bearer {header}"}, body) or header == secret


@FAST
@given(st.text(max_size=60))
def test_url_guard_only_raises_its_errors(url):
    fake_dns = [(2, 1, 6, "", ("93.184.216.34", 80))]  # no real DNS lookups in tests
    expected = (netguard.BlockedURL, urllib.error.URLError, ValueError, UnicodeError)
    with mock.patch("socket.getaddrinfo", return_value=fake_dns), contextlib.suppress(*expected):
        netguard.check_url("http://" + url)
