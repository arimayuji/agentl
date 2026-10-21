"""Cassettes (#211): record the HTTP responses and LLM answers of a run, replay them offline.

`agentl run --record DIR` keeps every response that went through `netguard.urlopen` and every
answer an agent step got, in `DIR/cassette.json`. `agentl test --replay DIR` serves them back:
no request leaves the machine and no LLM is called, so the test is deterministic.

Secrets are hidden before anything is written: credential headers, passwords and tokens in URLs,
and token or password fields in JSON and form bodies. Requests are matched on the redacted
method, URL and body, so a replay finds them without knowing the secrets.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import re
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .snapshots import REDACTED, redact_headers, redact_url

VERSION = 1
FILE = "cassette.json"

# Field names in bodies that hold a credential. Stricter than the header rule: a field named
# `author` or `session_date` is data, not a secret.
_SECRET_FIELD = re.compile(
    r"^(.*[_-])?(token|secret|password|passwd|api[_-]?key|apikey|client[_-]?secret|signature)([_-].*)?$",
    re.I,
)


class CassetteMiss(LookupError):
    """A replay asked for a request or an LLM answer the cassette does not have."""


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: REDACTED if isinstance(k, str) and _SECRET_FIELD.match(k) else _redact_value(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def redact_body(body: bytes | None) -> bytes:
    """A JSON or form body with token, password and key fields replaced by `***`."""
    if not body:
        return b""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        pass
    else:
        if isinstance(data, dict | list):
            return json.dumps(_redact_value(data), ensure_ascii=False).encode()
        return body
    try:
        text = body.decode()
    except UnicodeDecodeError:
        return body
    if re.fullmatch(r"[\w.~%+*-]+=[^&]*(&[\w.~%+*-]+=[^&]*)*", text):
        pairs = [p.partition("=") for p in text.split("&")]
        return "&".join(f"{k}={REDACTED if _SECRET_FIELD.match(k) else v}" for k, _, v in pairs).encode()
    return body


def _encode(body: bytes) -> dict[str, str]:
    try:
        return {"body": body.decode("utf-8")}
    except UnicodeDecodeError:
        return {"body": base64.b64encode(body).decode(), "encoding": "base64"}


def _decode(entry: dict) -> bytes:
    body = entry.get("body") or ""
    return base64.b64decode(body) if entry.get("encoding") == "base64" else body.encode("utf-8")


def _request_key(req: urllib.request.Request) -> str:
    data = req.data if isinstance(req.data, bytes) else None
    digest = hashlib.sha256(redact_body(data)).hexdigest()[:16] if data else ""
    return f"{req.get_method()} {redact_url(req.full_url)} {digest}".rstrip()


def _llm_key(kind: str, instruction: str, record: Any) -> str:
    text = json.dumps([kind, instruction, record], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def _headers(items: list[tuple[str, str]]) -> http.client.HTTPMessage:
    msg = http.client.HTTPMessage()
    for k, v in items:
        msg[k] = v
    return msg


class _Response(io.BytesIO):
    """What `urlopen` returns, rebuilt from a cassette entry."""

    def __init__(self, url: str, status: int, headers: http.client.HTTPMessage, body: bytes):
        super().__init__(body)
        self.url = url
        self.status = self.code = status
        self.headers = headers
        self.reason = http.client.responses.get(status, "")

    def geturl(self) -> str:
        return self.url

    def getcode(self) -> int:
        return self.status

    def info(self) -> http.client.HTTPMessage:
        return self.headers

    def getheader(self, name: str, default: Any = None) -> Any:
        return self.headers.get(name, default)


class Cassette:
    """The HTTP and LLM entries of one recording, in call order."""

    def __init__(self, directory: str | Path, mode: str):
        self.directory = Path(directory)
        self.mode = mode  # "record" or "replay"
        self.http: list[dict[str, Any]] = []
        self.llm: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._served: dict[str, int] = {}
        if mode == "replay":
            path = self.directory / FILE
            if not path.exists():
                raise FileNotFoundError(f"no cassette at {path} (record one with agentl run --record)")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.http, self.llm = list(data.get("http") or []), list(data.get("llm") or [])

    # http ---------------------------------------------------------------
    def urlopen(self, req: urllib.request.Request, real: Callable[[], Any]) -> Any:
        key = _request_key(req)
        if self.mode == "replay":
            return self._replay_http(key)
        try:
            resp = real()
        except urllib.error.HTTPError as e:
            body = e.read()
            self._add_http(key, req, e.code, list(e.headers.items()) if e.headers else [], body)
            raise urllib.error.HTTPError(e.url, e.code, e.msg, e.headers, io.BytesIO(body)) from None
        with resp:
            body = resp.read()
            status = getattr(resp, "status", 200)
            items = list(resp.headers.items()) if getattr(resp, "headers", None) else []
            url = resp.geturl() if hasattr(resp, "geturl") else req.full_url
        self._add_http(key, req, status, items, body)
        return _Response(url, status, _headers(items), body)

    def _add_http(
        self, key: str, req: urllib.request.Request, status: int, items: list[tuple[str, str]], body: bytes
    ) -> None:
        entry = {
            "key": key,
            "method": req.get_method(),
            "url": redact_url(req.full_url),
            "request_headers": redact_headers(dict(req.header_items())),
            "status": status,
            "headers": [[k, v] for k, v in redact_headers(dict(items)).items()],
            **_encode(redact_body(body)),
        }
        with self._lock:
            self.http.append(entry)

    def _replay_http(self, key: str) -> Any:
        with self._lock:
            found = [e for e in self.http if e.get("key") == key]
            if not found:
                raise CassetteMiss(f"cassette {self.directory}: no recorded response for {key}")
            n = self._served.get(key, 0)
            self._served[key] = n + 1
        entry = found[min(n, len(found) - 1)]  # repeated requests get the answers in order
        headers = _headers([(k, v) for k, v in entry.get("headers") or []])
        body, status = _decode(entry), int(entry.get("status") or 200)
        if status >= 400 or status == 304:
            raise urllib.error.HTTPError(entry["url"], status, "", headers, io.BytesIO(body))
        return _Response(entry["url"], status, headers, body)

    # llm ----------------------------------------------------------------
    def add_llm(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self.llm.append(entry)

    def answer(self, kind: str, instruction: str, record: Any) -> dict[str, Any]:
        key = _llm_key(kind, instruction, record)
        for e in self.llm:
            if e.get("key") == key:
                return e
        raise CassetteMiss(
            f"cassette {self.directory}: no recorded {kind} answer for this record and instruction "
            "(record again with agentl run --record)"
        )

    def save(self) -> Path:
        path = self.directory / FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {"version": VERSION, "http": self.http, "llm": self.llm}
        path.write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        return path


_active: Cassette | None = None


def active() -> Cassette | None:
    return _active


@contextmanager
def _use(c: Cassette) -> Iterator[Cassette]:
    global _active
    previous, _active = _active, c
    try:
        yield c
    finally:
        _active = previous


@contextmanager
def recording(directory: str | Path) -> Iterator[Cassette]:
    """Record every HTTP response and LLM answer inside the block; written on exit, also on error."""
    c = Cassette(directory, "record")
    try:
        with _use(c):
            yield c
    finally:
        c.save()


@contextmanager
def replaying(directory: str | Path) -> Iterator[Cassette]:
    """Serve HTTP responses and LLM answers from the cassette in `directory`; nothing goes out."""
    with _use(Cassette(directory, "replay")) as c:
        yield c


class CassetteProvider:
    """An LLM provider that records the answers of `inner`, or replays them without calling it.
    `step()` names the step the call is for, so recorded answers can become golden lines."""

    def __init__(self, cassette: Cassette, name: str, inner: Any, step: Callable[[], str | None]):
        self.cassette, self.name, self.inner, self.step = cassette, name, inner, step

    def __getattr__(self, attr: str) -> Any:
        if attr in ("rows", "structured_tools") and self.cassette.mode == "replay":
            return getattr(self, f"_{attr}")
        value = getattr(self.inner, attr)
        if attr in ("rows", "structured_tools"):
            return getattr(self, f"_{attr}")
        return value

    def _keep(self, kind: str, instruction: str, record: Any, answer: Any) -> None:
        self.cassette.add_llm(
            {
                "key": _llm_key(kind, instruction, record),
                "kind": kind,
                "llm": self.name,
                "step": self.step(),
                "instruction": instruction,
                "record": record,
                "answer": answer,
            }
        )

    def _structured(
        self, kind: str, call: Callable[[], Any], instruction: str, record: dict, schema: Any
    ) -> Any:
        from .llm import Result

        if self.cassette.mode == "replay":
            return Result(schema.model_validate(self.cassette.answer(kind, instruction, record)["answer"]))
        res = call()
        self._keep(kind, instruction, record, res.value.model_dump(mode="json"))
        return res

    def structured(self, instruction: str, record: dict, schema: Any) -> Any:
        return self._structured(
            "structured",
            lambda: self.inner.structured(instruction, record, schema),
            instruction,
            record,
            schema,
        )

    def _structured_tools(
        self, instruction: str, record: dict, schema: Any, tools: list, call: Any, max_calls: int
    ) -> Any:
        return self._structured(
            "structured",
            lambda: self.inner.structured_tools(instruction, record, schema, tools, call, max_calls),
            instruction,
            record,
            schema,
        )

    def _rows(self, instruction: str, record: dict, columns: list[str], previous: list[str]) -> Any:
        from .llm import RowsChunk

        asked = {"record": record, "columns": columns, "previous": previous}
        if self.cassette.mode == "replay":
            got = self.cassette.answer("rows", instruction, asked)["answer"]
            return RowsChunk(got["text"], bool(got.get("truncated")))
        res = self.inner.rows(instruction, record, columns, previous)
        self._keep("rows", instruction, asked, {"text": res.text, "truncated": res.truncated})
        return res

    def text(self, instruction: str, content: str) -> str:
        if self.cassette.mode == "replay":
            return str(self.cassette.answer("text", instruction, content)["answer"])
        out = self.inner.text(instruction, content)
        self._keep("text", instruction, content, out)
        return out


def golden_lines(directory: str | Path, step: str, outputs: list[str]) -> list[dict[str, Any]]:
    """The answers step `step` got in a recording, as golden lines: the record it read plus the
    answer's output fields. Check and correct them by hand before trusting them."""
    c = Cassette(directory, "replay")
    lines = []
    for e in c.llm:
        if e.get("step") != step or e.get("kind") != "structured" or not isinstance(e.get("record"), dict):
            continue
        rec = {k: v for k, v in e["record"].items() if not k.startswith("_")}
        answer = e.get("answer") or {}
        rec.update({k: v for k, v in answer.items() if k in outputs})
        lines.append(rec)
    return lines
