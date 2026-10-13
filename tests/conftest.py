import pytest


@pytest.fixture(autouse=True)
def _allow_local_network(monkeypatch):
    """Tests use local HTTP servers on 127.0.0.1; the SSRF guard tests remove this."""
    monkeypatch.setenv("AGENTL_ALLOW_PRIVATE_NETWORK", "1")
