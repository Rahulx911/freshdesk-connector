"""Tenant isolation. The property under test is that a merchant's data
cannot be reached with another merchant's token, and that the tenant can
never be chosen by anything the model says."""

from __future__ import annotations

import json

import pytest

from woocommerce_connector.errors import AuthError, ConfigError
from woocommerce_connector.tenancy import (
    Registry,
    RegistryWatcher,
    hash_token,
    mint_token,
)

TOKEN_A = "wcc_token_for_kettle_and_leaf"
TOKEN_B = "wcc_token_for_spice_route"


def registry_dict() -> dict:
    return {
        "tenants": {
            "kettle-and-leaf": {
                "store_url": "https://kettleandleaf.example.com",
                "consumer_key_env": "WOO_CK_KETTLE",
                "consumer_secret_env": "WOO_CS_KETTLE",
                "redact_pii": True,
            },
            "spice-route": {
                "store_url": "https://spiceroute.example.com",
                "consumer_key_env": "WOO_CK_SPICE",
                "consumer_secret_env": "WOO_CS_SPICE",
                "razorpay_key_id_env": "RZP_ID_SPICE",
                "razorpay_key_secret_env": "RZP_SECRET_SPICE",
                "redact_pii": False,
            },
        },
        "tokens": [
            {"name": "kettle-support", "tenant": "kettle-and-leaf", "sha256": hash_token(TOKEN_A)},
            {"name": "spice-support", "tenant": "spice-route", "sha256": hash_token(TOKEN_B)},
        ],
    }


@pytest.fixture
def registry() -> Registry:
    return Registry.from_dict(registry_dict())


# ------------------------------------------------------------- resolution
def test_token_resolves_to_its_own_tenant(registry):
    assert registry.resolve(TOKEN_A).tenant_id == "kettle-and-leaf"
    assert registry.resolve(TOKEN_B).tenant_id == "spice-route"


def test_one_merchants_token_never_reaches_another(registry):
    """The whole point of the module."""
    assert registry.resolve(TOKEN_A).store_url != registry.resolve(TOKEN_B).store_url


@pytest.mark.parametrize("bad", ["", "wcc_unknown", "not-a-token", TOKEN_A + "x", TOKEN_A.upper()])
def test_unknown_tokens_are_refused(registry, bad):
    with pytest.raises(AuthError):
        registry.resolve(bad)


def test_minted_tokens_are_unique_and_prefixed():
    tokens = {mint_token() for _ in range(100)}
    assert len(tokens) == 100
    assert all(t.startswith("wcc_") for t in tokens)


def test_only_hashes_are_stored(registry):
    blob = json.dumps(registry.describe())
    assert TOKEN_A not in blob
    assert TOKEN_B not in blob
    # describe() is for logs: it must not leak hashes either
    assert hash_token(TOKEN_A) not in blob


# -------------------------------------------------------------- secrets
@pytest.mark.parametrize("field", ["consumer_key", "consumer_secret",
                                   "razorpay_key_id", "razorpay_key_secret"])
def test_inline_secrets_are_rejected_not_warned_about(field):
    raw = registry_dict()
    raw["tenants"]["kettle-and-leaf"][field] = "ck_inline_secret"
    with pytest.raises(ConfigError) as e:
        Registry.from_dict(raw)
    assert field in str(e.value)


def test_credentials_come_from_the_environment(registry, monkeypatch):
    monkeypatch.setenv("WOO_CK_KETTLE", "ck_from_env")
    monkeypatch.setenv("WOO_CS_KETTLE", "cs_from_env")
    creds = registry.resolve(TOKEN_A).credentials()
    assert creds.consumer_key == "ck_from_env"
    assert creds.store_url == "https://kettleandleaf.example.com"


def test_missing_environment_variable_is_a_clear_error(registry, monkeypatch):
    monkeypatch.delenv("WOO_CK_KETTLE", raising=False)
    with pytest.raises(ConfigError) as e:
        registry.resolve(TOKEN_A).credentials()
    assert "WOO_CK_KETTLE" in str(e.value)


def test_razorpay_credentials_are_optional(registry, monkeypatch):
    """A merchant may have the store connected but not the gateway."""
    assert registry.resolve(TOKEN_A).razorpay_credentials() is None
    monkeypatch.setenv("RZP_ID_SPICE", "rzp_test_abc")
    monkeypatch.setenv("RZP_SECRET_SPICE", "secret")
    assert registry.resolve(TOKEN_B).razorpay_credentials().key_id == "rzp_test_abc"


def test_per_tenant_pii_setting_is_honoured(registry):
    assert registry.resolve(TOKEN_A).redact_pii is True
    assert registry.resolve(TOKEN_B).redact_pii is False


# ------------------------------------------------------------ validation
def test_token_for_an_unknown_tenant_is_rejected():
    raw = registry_dict()
    raw["tokens"].append({"name": "orphan", "tenant": "nope", "sha256": hash_token("x")})
    with pytest.raises(ConfigError):
        Registry.from_dict(raw)


def test_malformed_hash_is_rejected():
    raw = registry_dict()
    raw["tokens"][0]["sha256"] = "tooshort"
    with pytest.raises(ConfigError):
        Registry.from_dict(raw)


def test_tenant_without_a_store_url_is_rejected():
    raw = registry_dict()
    del raw["tenants"]["kettle-and-leaf"]["store_url"]
    with pytest.raises(ConfigError):
        Registry.from_dict(raw)


# --------------------------------------------------------------- reload
def test_revoking_a_token_takes_effect_without_a_restart(tmp_path):
    path = tmp_path / "tenants.json"
    path.write_text(json.dumps(registry_dict()))
    watcher = RegistryWatcher(path, interval_s=0.0)

    assert watcher.get().resolve(TOKEN_A).tenant_id == "kettle-and-leaf"

    revoked = registry_dict()
    revoked["tokens"] = [t for t in revoked["tokens"] if t["name"] != "kettle-support"]
    path.write_text(json.dumps(revoked))
    import os
    os.utime(path, (1, 1))          # force a different mtime

    with pytest.raises(AuthError):
        watcher.get().resolve(TOKEN_A)


def test_a_broken_registry_keeps_serving_the_last_good_copy(tmp_path):
    """Losing the file must not take every merchant offline at once."""
    path = tmp_path / "tenants.json"
    path.write_text(json.dumps(registry_dict()))
    watcher = RegistryWatcher(path, interval_s=0.0)
    assert watcher.get().resolve(TOKEN_A)

    path.unlink()
    assert watcher.get().resolve(TOKEN_A).tenant_id == "kettle-and-leaf"


def test_a_missing_registry_on_first_load_is_fatal(tmp_path):
    watcher = RegistryWatcher(tmp_path / "absent.json", interval_s=0.0)
    with pytest.raises(ConfigError):
        watcher.get()


# ------------------------------------------------- hosted service provider
async def test_each_tenant_gets_its_own_client_and_cache(tmp_path, monkeypatch):
    """One busy merchant must not drain or read another's budget or cache."""
    from woocommerce_connector.mcp_server import TenantServiceProvider
    from woocommerce_connector.tenancy import RegistryWatcher

    path = tmp_path / "tenants.json"
    path.write_text(json.dumps(registry_dict()))
    for var, value in [("WOO_CK_KETTLE", "ck_k"), ("WOO_CS_KETTLE", "cs_k"),
                       ("WOO_CK_SPICE", "ck_s"), ("WOO_CS_SPICE", "cs_s")]:
        monkeypatch.setenv(var, value)

    provider = TenantServiceProvider(RegistryWatcher(path, interval_s=0.0))
    a = await provider.for_token(TOKEN_A)
    b = await provider.for_token(TOKEN_B)

    assert a is not b
    assert a.client is not b.client
    assert a.client.cache is not b.client.cache
    assert a.client.rl is not b.client.rl
    assert a.store_url != b.store_url
    # the per-tenant PII setting is applied, not the default
    assert a.redact_pii is True and b.redact_pii is False
    await provider.aclose()


async def test_the_same_token_reuses_one_service(tmp_path, monkeypatch):
    from woocommerce_connector.mcp_server import TenantServiceProvider
    from woocommerce_connector.tenancy import RegistryWatcher

    path = tmp_path / "tenants.json"
    path.write_text(json.dumps(registry_dict()))
    monkeypatch.setenv("WOO_CK_KETTLE", "ck_k")
    monkeypatch.setenv("WOO_CS_KETTLE", "cs_k")
    provider = TenantServiceProvider(RegistryWatcher(path, interval_s=0.0))
    assert await provider.for_token(TOKEN_A) is await provider.for_token(TOKEN_A)
    await provider.aclose()


async def test_hosted_mode_refuses_a_tenantless_call(tmp_path):
    """get() without a token must fail loudly rather than pick a default."""
    from woocommerce_connector.errors import ConfigError as CE
    from woocommerce_connector.mcp_server import TenantServiceProvider
    from woocommerce_connector.tenancy import RegistryWatcher

    path = tmp_path / "tenants.json"
    path.write_text(json.dumps(registry_dict()))
    provider = TenantServiceProvider(RegistryWatcher(path, interval_s=0.0))
    with pytest.raises(CE) as e:
        await provider.get()
    assert "never taken from a tool argument" in str(e.value)


async def test_an_unknown_token_cannot_reach_any_merchant(tmp_path):
    from woocommerce_connector.mcp_server import TenantServiceProvider
    from woocommerce_connector.tenancy import RegistryWatcher

    path = tmp_path / "tenants.json"
    path.write_text(json.dumps(registry_dict()))
    provider = TenantServiceProvider(RegistryWatcher(path, interval_s=0.0))
    with pytest.raises(AuthError):
        await provider.for_token("wcc_forged")
