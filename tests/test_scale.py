"""Behaviour at merchant scale, where the fixture stops being representative.

The mock store has ten orders. A real merchant has thousands in the same
window, and the failure mode that matters is not a crash: it is a confident
rupee figure computed from one page of results.
"""

from __future__ import annotations

import httpx
import pytest

from woocommerce_connector.auth import Credentials
from woocommerce_connector.client import WooClient
from woocommerce_connector.errors import InvalidRequest
from woocommerce_connector.ratelimit import RateLimiter
from woocommerce_connector.service import WooService

CREDS = Credentials("https://shop.example.com", "ck_x", "cs_y")


def big_store(total: int, per_page_cap: int = 100):
    """A store with `total` orders, half of them unconfirmed refunds."""
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        per_page = min(int(request.url.params.get("per_page", 10)), per_page_cap)
        start = (page - 1) * per_page
        rows = []
        for i in range(start, min(start + per_page, total)):
            rows.append({
                "id": 1000 + i, "number": str(1000 + i), "status": "refunded",
                "currency": "INR", "total": "100.00",
                "date_created_gmt": "2026-09-20T10:00:00",
                "payment_method": "razorpay", "payment_method_title": "Razorpay",
                "transaction_id": f"pay_AAAAAAAAAA{i:04d}",
                "meta_data": [], "line_items": [],
                # A refund row with no gateway refund id: money at risk.
                "refunds": [{"id": 9000 + i, "reason": "", "total": "-100.00"}],
            })
        total_pages = max(1, (total + per_page - 1) // per_page)
        headers = {"X-WP-Total": str(total), "X-WP-TotalPages": str(total_pages)}
        if page < total_pages:
            headers["Link"] = f'<https://shop.example.com/?page={page + 1}>; rel="next"'
        return httpx.Response(200, json=rows, headers=headers)
    return handler


async def service_for(total: int, **kwargs):
    client = WooClient(CREDS, transport=httpx.MockTransport(big_store(total)),
                       rate_limiter=RateLimiter(100_000))
    return WooService(client), client


async def test_pulse_pages_through_the_whole_window():
    """450 orders must not be reported from the first 100."""
    svc, client = await service_for(450)
    pulse = await svc.store_pulse(days=30, scan=100, max_pages=10)
    assert pulse["coverage"]["orders_scanned"] == 450
    assert pulse["coverage"]["scan_complete"] is True
    assert pulse["coverage"]["pages_fetched"] == 5
    assert pulse["money_at_risk"]["total_exposed"] == 45000.0
    await client.aclose()


async def test_partial_scan_refuses_to_state_a_total():
    """The failure this guards: a lower bound quoted as a total."""
    svc, client = await service_for(4000)
    pulse = await svc.store_pulse(days=30, scan=100, max_pages=2)
    cov = pulse["coverage"]
    assert cov["scan_complete"] is False
    assert cov["orders_scanned"] == 200
    assert cov["orders_matching_window"] == 4000
    assert "LOWER BOUND" in cov["warning"]

    risk = pulse["money_at_risk"]
    # The key name itself must change, so a partial figure cannot be read
    # as a complete one by anything downstream.
    assert "total_exposed" not in risk
    assert risk["total_exposed_lower_bound"] == 20000.0
    assert risk["scan_complete"] is False
    assert "do not quote" in risk["warning"].lower()
    await client.aclose()


async def test_partial_scan_renames_the_headline_totals_too():
    svc, client = await service_for(4000)
    pulse = await svc.store_pulse(days=30, scan=100, max_pages=1)
    assert "gross_value" not in pulse
    assert "gross_value_lower_bound" in pulse
    assert "refunded_value" not in pulse
    await client.aclose()


async def test_complete_scan_keeps_the_plain_key_names():
    svc, client = await service_for(40)
    pulse = await svc.store_pulse(days=30, scan=100)
    assert "gross_value" in pulse
    assert "total_exposed" in pulse["money_at_risk"]
    assert "warning" not in pulse["coverage"]
    await client.aclose()


async def test_max_pages_is_bounded_so_one_call_cannot_walk_a_whole_store():
    svc, client = await service_for(100_000)
    with pytest.raises(InvalidRequest):
        await svc.store_pulse(days=30, max_pages=500)
    await client.aclose()


async def test_pagination_stops_early_on_an_empty_page():
    """A store that reports has_more but returns nothing must not loop."""
    def handler(request):
        return httpx.Response(200, json=[], headers={
            "X-WP-Total": "9999", "X-WP-TotalPages": "99",
            "Link": '<https://shop.example.com/?page=2>; rel="next"'})

    client = WooClient(CREDS, transport=httpx.MockTransport(handler),
                       rate_limiter=RateLimiter(100_000))
    pulse = await WooService(client).store_pulse(days=30, max_pages=10)
    assert pulse["coverage"]["pages_fetched"] == 1
    assert pulse["coverage"]["orders_scanned"] == 0
    await client.aclose()
