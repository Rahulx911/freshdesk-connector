import os
import stat

import pytest

from freshdesk_connector.auth import Credentials, CredentialStore, normalize_base_url, resolve_credentials
from freshdesk_connector.errors import AuthError, ConfigError


@pytest.mark.parametrize("inp,out", [
    ("acme", "https://acme.freshdesk.com"),
    ("acme.freshdesk.com", "https://acme.freshdesk.com"),
    ("https://acme.freshdesk.com/", "https://acme.freshdesk.com"),
    ("http://127.0.0.1:8765", "http://127.0.0.1:8765"),
    ("support.acme.in", "https://support.acme.in"),
])
def test_normalize_base_url(inp, out):
    assert normalize_base_url(inp) == out


@pytest.mark.parametrize("bad", ["", "http://evil.example.com", "acme freshdesk", "../etc",
                                 "https://10.0.0.5", "https://169.254.169.254", "https://redis.svc.cluster.local",
                                 "https://metadata.internal", "https://acme.freshdesk.com/api/v2", "http://mock-freshdesk:8765",
                                 "https://[::1]"])
def test_normalize_rejects_bad_domains(bad):
    with pytest.raises(ConfigError):
        normalize_base_url(bad)


def test_store_is_private_and_roundtrips(tmp_path):
    store = CredentialStore(tmp_path / "c" / "credentials.json")
    creds = Credentials(base_url="https://acme.freshdesk.com", api_key="abcdefghijkl", agent_name="A")
    store.save(creds)
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert store.load() == creds
    assert store.delete() and store.load() is None


def test_key_never_in_repr():
    creds = Credentials(base_url="https://x.freshdesk.com", api_key="supersecretkey123")
    assert "supersecretkey123" not in repr(creds)
    assert "supersecretkey123" not in str(creds.redacted())


def test_env_overrides_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FRESHDESK_DOMAIN", "acme")
    monkeypatch.setenv("FRESHDESK_API_KEY", "k1")
    c = resolve_credentials(CredentialStore(tmp_path / "none.json"))
    assert c.base_url == "https://acme.freshdesk.com" and c.api_key == "k1"


def test_missing_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("FRESHDESK_DOMAIN", raising=False)
    monkeypatch.delenv("FRESHDESK_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        resolve_credentials(CredentialStore(tmp_path / "none.json"))


async def test_invalid_key_is_auth_error(make_service):
    svc = make_service(api_key="wrong-key")
    with pytest.raises(AuthError) as e:
        await svc.connector_status()
    assert e.value.to_dict()["error"] == "auth_failed"


async def test_valid_key_whoami(make_service):
    status = await make_service().connector_status()
    assert status["connected"] and status["access"] == "read-only"
    assert status["authenticated_as"]["name"] == "Demo Support Agent"
    assert "api_key" not in str(status)


def test_insecure_http_host_is_opt_in(monkeypatch):
    monkeypatch.setenv("FRESHDESK_INSECURE_HTTP_HOSTS", "mock-freshdesk")
    assert normalize_base_url("http://mock-freshdesk:8765") == "http://mock-freshdesk:8765"
    with pytest.raises(ConfigError):
        normalize_base_url("http://other-host:8765")
