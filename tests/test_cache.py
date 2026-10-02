"""The response cache. Stale money data is worse than slow money data, so
this is deliberately conservative and the tests pin that."""

from __future__ import annotations

import httpx

from woocommerce_connector.auth import Credentials
from woocommerce_connector.cache import ResponseCache, cache_key
from woocommerce_connector.client import WooClient
from woocommerce_connector.ratelimit import RateLimiter

CREDS = Credentials("https://shop.example.com", "ck_x", "cs_y")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def counting_store() -> tuple[httpx.MockTransport, dict]:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=[{"id": 1}], headers={"X-WP-Total": "1"})

    return httpx.MockTransport(handler), calls


# ------------------------------------------------------------------- unit
def test_hit_and_miss_are_distinguishable_from_a_cached_none():
    c = ResponseCache()
    assert c.get("absent") == (False, None)
    c.put("k", None)
    assert c.get("k") == (True, None)


def test_entries_expire():
    clock = Clock()
    c = ResponseCache(ttl_s=10, now=clock)
    c.put("k", "v")
    clock.t = 9.9
    assert c.get("k")[0] is True
    clock.t = 10.1
    assert c.get("k")[0] is False


def test_zero_ttl_disables_caching():
    c = ResponseCache(ttl_s=0)
    c.put("k", "v")
    assert c.get("k")[0] is False


def test_least_recently_used_entries_are_evicted():
    c = ResponseCache(max_entries=2)
    c.put("a", 1)
    c.put("b", 2)
    c.get("a")          # a is now most recent
    c.put("c", 3)       # evicts b
    assert c.get("a")[0] is True
    assert c.get("b")[0] is False
    assert c.get("c")[0] is True


def test_oauth_noise_is_excluded_from_the_key():
    """Nonce, timestamp and signature change every request. If they entered
    the key nothing would ever hit."""
    a = cache_key("scope", "/orders", {"per_page": 1, "oauth_nonce": "aaa",
                                       "oauth_timestamp": "1", "oauth_signature": "x"})
    b = cache_key("scope", "/orders", {"per_page": 1, "oauth_nonce": "bbb",
                                       "oauth_timestamp": "2", "oauth_signature": "y"})
    assert a == b


def test_different_credentials_never_share_an_entry():
    """Two merchants in one process must not read each other's orders."""
    a = cache_key("https://a.example.com|ck_a", "/orders", {"per_page": 1})
    b = cache_key("https://b.example.com|ck_b", "/orders", {"per_page": 1})
    assert a != b


def test_different_params_are_different_entries():
    assert cache_key("s", "/orders", {"status": "failed"}) != \
           cache_key("s", "/orders", {"status": "refunded"})


# ------------------------------------------------------------ integration
async def test_repeated_reads_hit_the_store_once():
    transport, calls = counting_store()
    client = WooClient(CREDS, transport=transport, rate_limiter=RateLimiter(10_000))
    for _ in range(3):
        await client.get_page("/orders", {"per_page": 1})
    assert calls["n"] == 1
    assert client.cache.stats()["hits"] == 2
    await client.aclose()


async def test_fresh_bypasses_the_cache():
    transport, calls = counting_store()
    client = WooClient(CREDS, transport=transport, rate_limiter=RateLimiter(10_000))
    await client.get_page("/orders", {"per_page": 1})
    await client.get_page("/orders", {"per_page": 1}, fresh=True)
    assert calls["n"] == 2
    await client.aclose()


async def test_expired_entries_refetch():
    clock = Clock()
    transport, calls = counting_store()
    client = WooClient(CREDS, transport=transport, rate_limiter=RateLimiter(10_000),
                       cache=ResponseCache(ttl_s=30, now=clock))
    await client.get_page("/orders", {"per_page": 1})
    clock.t = 31
    await client.get_page("/orders", {"per_page": 1})
    assert calls["n"] == 2
    await client.aclose()


async def test_errors_are_never_cached():
    """A 500 must not be remembered as the answer for the next 30 seconds."""
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(500, json={})
        return httpx.Response(200, json=[{"id": 1}], headers={"X-WP-Total": "1"})

    async def nosleep(_):
        return None

    client = WooClient(CREDS, transport=httpx.MockTransport(handler),
                       rate_limiter=RateLimiter(10_000), sleep=nosleep)
    data, _ = await client.get_page("/orders")
    assert data == [{"id": 1}]
    assert state["n"] == 2       # the 500 was retried, not cached
    await client.aclose()


async def test_status_reports_cache_health(service):
    st = await service.connector_status()
    assert "cache" in st
    assert st["cache"]["ttl_seconds"] > 0
