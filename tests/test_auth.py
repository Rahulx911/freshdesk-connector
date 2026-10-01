from __future__ import annotations

import os
import stat

import pytest

from woocommerce_connector import auth
from woocommerce_connector.errors import ConfigError


@pytest.mark.parametrize("raw,expected", [
    ("shop.example.com", "https://shop.example.com"),
    ("https://shop.example.com/", "https://shop.example.com"),
    ("https://shop.example.com/wp-json/wc/v3", "https://shop.example.com"),
    ("http://localhost:8787", "http://localhost:8787"),
    ("http://127.0.0.1:8080/", "http://127.0.0.1:8080"),
    ("http://woo.test", "http://woo.test"),
])
def test_normalize_accepts(raw, expected):
    assert auth.normalize_store_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "",
    "ftp://shop.example.com",
    "http://shop.example.com",          # plain http to a public host
    "https://169.254.169.254",          # raw IP
    "https://metadata.google.internal",
    "https://intranet",                 # no dot, not local
])
def test_normalize_rejects(raw):
    with pytest.raises(ConfigError):
        auth.normalize_store_url(raw)


def test_secret_never_in_redacted_output():
    c = auth.Credentials("https://shop.example.com", "ck_" + "a" * 40, "cs_" + "b" * 40)
    blob = repr(c.redacted())
    assert "cs_" not in blob
    assert "a" * 40 not in blob
    assert c.redacted()["consumer_key"].startswith("ck_")


def test_basic_auth_only_over_https():
    """WooCommerce ignores Basic auth without TLS and falls through to OAuth,
    so the scheme must follow the transport, not the caller's preference."""
    https = auth.Credentials("https://shop.example.com", "ck_x", "cs_y")
    local = auth.Credentials("http://localhost:8787", "ck_x", "cs_y")
    assert https.uses_basic_auth and https.auth_scheme == "basic"
    assert not local.uses_basic_auth
    assert local.auth_scheme == "oauth1"


def test_store_is_written_0600(tmp_path, monkeypatch):
    monkeypatch.setenv("WOO_CONNECTOR_HOME", str(tmp_path / "cfg"))
    path = auth.save(auth.Credentials("https://shop.example.com", "ck_x", "cs_y"))
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600
    loaded = auth.load()
    assert loaded.consumer_secret == "cs_y"
    assert auth.clear() is True


def test_env_beats_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("WOO_CONNECTOR_HOME", str(tmp_path / "cfg"))
    auth.save(auth.Credentials("https://disk.example.com", "ck_disk", "cs_disk"))
    monkeypatch.setenv(auth.ENV_URL, "https://env.example.com")
    monkeypatch.setenv(auth.ENV_KEY, "ck_env")
    monkeypatch.setenv(auth.ENV_SECRET, "cs_env")
    assert auth.load().store_url == "https://env.example.com"


def test_api_base_is_the_woocommerce_namespace():
    c = auth.Credentials("https://shop.example.com", "ck_x", "cs_y")
    assert c.api_base() == "https://shop.example.com/wp-json/wc/v3"
