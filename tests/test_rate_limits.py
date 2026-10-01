from __future__ import annotations

import httpx
import pytest

from woocommerce_connector.auth import Credentials
from woocommerce_connector.client import WooClient
from woocommerce_connector.errors import RateLimited, UpstreamError
from woocommerce_connector.ratelimit import RateLimiter

CREDS = Credentials("https://shop.example.com", "ck_x", "cs_y")


async def nosleep(seconds: float) -> None:
    """Collapse backoff waits so the suite stays fast."""
    return None


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def test_budget_holds_back_a_reserve():
    rl = RateLimiter(100, reserve=0.2)
    assert rl.usable == 80


async def test_budget_blocks_when_exhausted_and_reports_wait():
    clock = Clock()
    rl = RateLimiter(10, reserve=0.0, now=clock)
    for _ in range(10):
        entry, wait = await rl.acquire(1)
        assert entry is not None
        await rl.settle(entry)
    entry, wait = await rl.acquire(1)
    assert entry is None and wait > 0


async def test_budget_recovers_after_the_window():
    clock = Clock()
    rl = RateLimiter(2, reserve=0.0, now=clock)
    for _ in range(2):
        e, _ = await rl.acquire(1)
        await rl.settle(e)
    assert (await rl.acquire(1))[0] is None
    clock.t += 61
    assert (await rl.acquire(1))[0] is not None


async def test_limit_is_learned_from_store_headers():
    rl = RateLimiter(50)
    await rl.settle(None, httpx.Headers({"RateLimit-Limit": "600", "RateLimit-Remaining": "412"}))
    stats = await rl.stats()
    assert stats["limit_per_min"] == 600
    assert stats["limit_source"] == "store headers"
    assert stats["store_reported_remaining"] == 412


async def test_x_prefixed_headers_also_work():
    rl = RateLimiter(50)
    await rl.settle(None, httpx.Headers({"X-RateLimit-Limit": "300"}))
    assert (await rl.stats())["limit_per_min"] == 300


async def test_429_is_retried_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"},
                                  json={"code": "too_many_requests", "message": "slow down",
                                        "data": {"status": 429}})
        return httpx.Response(200, json=[], headers={"X-WP-Total": "0"})

    slept = []

    async def sleep(s):
        slept.append(s)

    c = WooClient(CREDS, transport=httpx.MockTransport(handler), max_wait_s=30, sleep=sleep)
    await c.get("/orders")
    assert calls["n"] == 2 and slept == [1.0]
    await c.aclose()


async def test_503_is_treated_as_throttling():
    def handler(request):
        return httpx.Response(503, headers={"Retry-After": "900"}, json={})

    c = WooClient(CREDS, transport=httpx.MockTransport(handler), max_wait_s=5,
                  sleep=nosleep)
    with pytest.raises(RateLimited) as e:
        await c.get("/orders")
    assert e.value.to_dict()["retry_after_seconds"] == 900.0
    await c.aclose()


async def test_long_wait_fails_fast_with_structured_error():
    """An agent must never block a conversation for ten minutes."""
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "600"}, json={})

    c = WooClient(CREDS, transport=httpx.MockTransport(handler), max_wait_s=5,
                  sleep=nosleep)
    with pytest.raises(RateLimited) as e:
        await c.get("/orders")
    payload = e.value.to_dict()
    assert payload["error"] == "rate_limited"
    assert payload["retry_after_seconds"] == 600.0
    assert "wait" in payload["hint"].lower()
    await c.aclose()


async def test_5xx_backs_off_then_gives_up():
    def handler(request):
        return httpx.Response(500, json={})

    c = WooClient(CREDS, transport=httpx.MockTransport(handler), max_wait_s=30,
                  max_retries=2, sleep=nosleep)
    with pytest.raises(UpstreamError):
        await c.get("/orders")
    await c.aclose()


async def test_redirect_is_refused_so_the_secret_is_not_forwarded():
    def handler(request):
        return httpx.Response(301, headers={"Location": "https://evil.example.com/wp-json"})

    c = WooClient(CREDS, transport=httpx.MockTransport(handler), sleep=nosleep)
    with pytest.raises(Exception) as e:
        await c.get("/orders")
    assert "redirect" in str(e.value).lower()
    await c.aclose()
