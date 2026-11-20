import pytest
from tests.test_api import client  # noqa: F401  (fixture)

from transcriptd import tailnet
from transcriptd.api import deps


@pytest.fixture
def fake_tailnet(monkeypatch):
    logins = {"100.64.0.1": "me@example.com", "100.64.0.2": "someone-else@example.com"}
    monkeypatch.setattr(tailnet, "_me", lambda: ("me@example.com", frozenset({"100.64.0.3"})))
    monkeypatch.setattr(tailnet, "login_of", lambda ip: logins.get(ip))


def test_only_my_own_devices_are_trusted(fake_tailnet):
    assert tailnet.trusted("100.64.0.1", [])
    assert not tailnet.trusted("100.64.0.2", [])  # another user on the same tailnet
    assert tailnet.trusted("100.64.0.2", ["someone-else@example.com"])
    assert not tailnet.trusted("100.64.0.3", [])  # this machine (and tailscale serve's proxy)
    assert not tailnet.trusted("192.168.1.20", [])  # not a tailnet address
    assert not tailnet.trusted("100.64.0.9", [])  # tailnet address nobody answers for
    assert not tailnet.trusted(None, [])


def test_tailnet_auth_is_opt_in(client, fake_tailnet, monkeypatch):
    seen = []
    monkeypatch.setattr(deps.tailnet, "trusted", lambda ip, users: seen.append(ip) or True)
    client.headers.pop("Authorization", None)
    assert client.get("/v1/health").status_code == 401 and not seen  # off: never consulted
    client.app.state.settings.tailnet_auth = True
    assert client.get("/v1/health", headers={"Host": "100.64.0.3:7411"}).status_code == 200
    assert client.get("/v1/health").status_code == 403  # Host "testserver": not one of our names
