"""Hardening from the security audit (#104): config URLs, webhook edge cases."""

import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest

from agentl import parse
from agentl.llm import provider
from agentl.spec import LLMSpec, MCPServerSpec
from agentl.store import Store
from agentl.triggers import Service


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com", "gopher://x"])
def test_config_urls_must_be_http(url):
    with pytest.raises(ValueError, match="must be an http"):
        provider(LLMSpec(provider="openai-compatible", model="m", options={"base_url": url}))
    from agentl.mcp import MCPClient

    with pytest.raises(ValueError, match="must be an http"):
        MCPClient("x", MCPServerSpec(url=url))
    from agentl.notify import _post

    with pytest.raises(ValueError, match="must be an http"):
        _post(url, {})


def test_webhook_rejects_bad_content_length(tmp_path):
    spec = parse({"name": "p", "trigger": [{"webhook": "/h"}], "steps": [{"id": "a", "use": "trigger"}]})
    with Store(tmp_path / "s.db") as store:
        svc = Service([spec], store, run=lambda *a: None)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), svc.handler())
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            for value in ("-1", "abc"):
                c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
                c.putrequest("POST", "/h")
                c.putheader("Content-Length", value)
                c.endheaders()
                assert c.getresponse().status == 400
        finally:
            srv.shutdown()
            srv.server_close()
    assert svc.handler().timeout == 30


def test_open_webhook_on_a_public_host_warns(tmp_path, capsys, monkeypatch):
    spec = parse({"name": "p", "trigger": [{"webhook": "/h"}], "steps": [{"id": "a", "use": "trigger"}]})
    with Store(tmp_path / "s.db") as store:
        svc = Service([spec], store, run=lambda *a: None)
        svc.stop()  # serve returns right away
        svc.serve("0.0.0.0", 0, interval=0.01)
    assert "webhooks without a secret are open" in capsys.readouterr().err
