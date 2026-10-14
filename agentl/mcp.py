"""Minimal MCP client (Model Context Protocol) for agent step tools: stdio and streamable HTTP,
stdlib only. It speaks just what tools need: initialize, tools/list and tools/call."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from typing import Any

from .spec import MCPServerSpec

PROTOCOL_VERSION = "2025-06-18"


class MCPError(RuntimeError):
    pass


class _Transport:
    def request(self, method: str, params: dict) -> dict:
        raise NotImplementedError

    def notify(self, method: str, params: dict) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class _Stdio(_Transport):
    """A local server started as a subprocess; newline-delimited JSON-RPC on stdin/stdout."""

    def __init__(self, command: list[str], env: dict[str, str], timeout: float):
        self.timeout = timeout
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**os.environ, **env},
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._next = 0
        self._lock = threading.Lock()
        self._waiting: dict[int, queue.Queue] = {}
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # servers may log to stdout; ignore what is not JSON-RPC
            rid = msg.get("id") if isinstance(msg, dict) else None
            box = self._waiting.pop(rid, None) if isinstance(rid, int) else None
            if box is not None:
                box.put(msg)
        for box in list(self._waiting.values()):
            box.put({"error": {"message": "the MCP server exited"}})

    def _send(self, msg: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        box: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            self._next += 1
            rid = self._next
            self._waiting[rid] = box
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            msg = box.get(timeout=self.timeout)
        except queue.Empty as e:
            self._waiting.pop(rid, None)
            raise MCPError(f"{method}: no answer after {self.timeout:.0f}s") from e
        if "error" in msg:
            raise MCPError(f"{method}: {msg['error'].get('message', msg['error'])}")
        return msg.get("result") or {}

    def notify(self, method: str, params: dict) -> None:
        with self._lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()


class _Http(_Transport):
    """Streamable HTTP: JSON-RPC over POST; the answer is JSON or a short SSE stream."""

    def __init__(self, url: str, headers: dict[str, str], timeout: float):
        self.url = url
        self.headers = headers
        self.timeout = timeout
        self.session: str | None = None
        self._next = 0
        self._lock = threading.Lock()

    def _post(self, msg: dict) -> Any:
        import urllib.request  # imported here: most pipelines never use MCP

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            **self.headers,
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, json.dumps(msg).encode(), headers, method="POST")
        return urllib.request.urlopen(req, timeout=self.timeout)

    def request(self, method: str, params: dict) -> dict:
        import urllib.error

        with self._lock:
            self._next += 1
            rid = self._next
        try:
            resp = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except urllib.error.HTTPError as e:
            raise MCPError(f"{method}: HTTP {e.code}") from e
        with resp:
            self.session = resp.headers.get("Mcp-Session-Id") or self.session
            body = resp.read().decode("utf-8", errors="replace")
            ctype = resp.headers.get("Content-Type", "")
        messages = (
            [
                json.loads(line[5:])
                for line in body.splitlines()
                if line.startswith("data:") and line[5:].strip()
            ]
            if "text/event-stream" in ctype
            else [json.loads(body)]
        )
        for msg in messages:
            if isinstance(msg, dict) and msg.get("id") == rid:
                if "error" in msg:
                    raise MCPError(f"{method}: {msg['error'].get('message', msg['error'])}")
                return msg.get("result") or {}
        raise MCPError(f"{method}: no answer in the response")

    def notify(self, method: str, params: dict) -> None:
        self._post({"jsonrpc": "2.0", "method": method, "params": params}).close()


class MCPClient:
    def __init__(self, name: str, spec: MCPServerSpec):
        from . import __version__

        self.name = name
        if spec.command:
            self.transport: _Transport = _Stdio(spec.command, spec.env, spec.timeout)
        else:
            self.transport = _Http(spec.url or "", spec.headers, spec.timeout)
        self.transport.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agentl", "version": __version__},
            },
        )
        self.transport.notify("notifications/initialized", {})

    def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        while True:
            page = self.transport.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += page.get("tools", [])
            cursor = page.get("nextCursor")
            if not cursor:
                return tools

    def call(self, tool: str, arguments: dict) -> str:
        """The tool's text content. A tool error comes back as text too, prefixed with `error:`,
        so the model can react to it."""
        res = self.transport.request("tools/call", {"name": tool, "arguments": arguments})
        parts = []
        for block in res.get("content", []):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(json.dumps(block)[:2000])
        if not parts and "structuredContent" in res:
            parts.append(json.dumps(res["structuredContent"]))
        text = "\n".join(parts)
        return f"error: {text}" if res.get("isError") else text

    def close(self) -> None:
        self.transport.close()
