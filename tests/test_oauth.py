"""OAuth 1.0a one-legged signing, checked against the mock's verifier.

The mock implements WooCommerce's `check_oauth_signature()` independently, so
these tests catch a signer that merely agrees with itself. The real store was
used to confirm both sides are right.
"""

from __future__ import annotations

import httpx
import pytest

from mock_server import data as mock_data
from mock_server.app import app as mock_app
from woocommerce_connector.auth import Credentials
from woocommerce_connector.client import WooClient
from woocommerce_connector.oauth import rfc3986, sign, signature_base_string
from woocommerce_connector.ratelimit import RateLimiter

# A plain-HTTP local store: the case where WooCommerce requires a signature.
LOCAL = Credentials("http://localhost:8787", mock_data.CONSUMER_KEY, mock_data.CONSUMER_SECRET)


def test_base_string_joins_pairs_the_woocommerce_way():
    """Pairs are encoded whole, so `=` becomes %3D, and joined with %26."""
    base = signature_base_string("GET", "http://localhost:8080/wp-json/wc/v3/orders",
                                 {"per_page": 1, "oauth_nonce": "abc"})
    assert base.startswith("GET&http%3A%2F%2Flocalhost%3A8080")
    assert "oauth_nonce%3Dabc%26per_page%3D1" in base
    assert "&per_page=" not in base


def test_parameters_are_sorted():
    a = signature_base_string("GET", "http://x.test/y", {"b": 2, "a": 1})
    b = signature_base_string("GET", "http://x.test/y", {"a": 1, "b": 2})
    assert a == b
    assert a.index("a%3D1") < a.index("b%3D2")


def test_signature_excluded_from_its_own_base_string():
    params = {"a": 1, "oauth_signature": "should-be-ignored"}
    assert "should-be-ignored" not in signature_base_string("GET", "http://x.test/y", params)


def test_rfc3986_leaves_tilde_unescaped():
    assert rfc3986("a~b") == "a~b"
    assert rfc3986("a b") == "a%20b"
    assert rfc3986("a/b") == "a%2Fb"


def test_signing_is_deterministic_for_a_fixed_nonce():
    kwargs = dict(timestamp=1700000000, nonce="fixednonce")
    one = sign("GET", "http://x.test/y", {"a": 1}, "ck", "cs", **kwargs)
    two = sign("GET", "http://x.test/y", {"a": 1}, "ck", "cs", **kwargs)
    assert one["oauth_signature"] == two["oauth_signature"]


def test_a_different_secret_gives_a_different_signature():
    kwargs = dict(timestamp=1700000000, nonce="fixednonce")
    one = sign("GET", "http://x.test/y", {}, "ck", "secret-one", **kwargs)
    two = sign("GET", "http://x.test/y", {}, "ck", "secret-two", **kwargs)
    assert one["oauth_signature"] != two["oauth_signature"]


def test_unknown_signature_method_rejected():
    with pytest.raises(ValueError):
        sign("GET", "http://x.test/y", {}, "ck", "cs", signature_method="PLAINTEXT")


# ------------------------------------------------- end to end against the mock
async def test_plain_http_store_authenticates_with_a_signature():
    client = WooClient(LOCAL, transport=httpx.ASGITransport(app=mock_app),
                       rate_limiter=RateLimiter(10_000))
    data, info = await client.get_page("/orders", {"per_page": 2})
    assert info["total"] == 10
    assert len(data) == 2
    await client.aclose()


async def test_a_wrong_secret_is_refused_by_the_store():
    bad = Credentials("http://localhost:8787", mock_data.CONSUMER_KEY, "cs_wrong")
    client = WooClient(bad, transport=httpx.ASGITransport(app=mock_app),
                       rate_limiter=RateLimiter(10_000))
    with pytest.raises(Exception) as exc:
        await client.get_page("/orders")
    assert getattr(exc.value, "code", "") in ("auth_failed", "permission_denied")
    await client.aclose()


async def test_https_store_uses_basic_auth_not_a_signature():
    https = Credentials("https://shop.example.com", mock_data.CONSUMER_KEY,
                        mock_data.CONSUMER_SECRET)
    client = WooClient(https, transport=httpx.ASGITransport(app=mock_app),
                       rate_limiter=RateLimiter(10_000))
    resp = await client.get("/orders", {"per_page": 1})
    assert "oauth_signature" not in str(resp.request.url)
    await client.aclose()
