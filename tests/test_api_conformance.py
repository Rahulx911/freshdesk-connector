"""Facts about the real WooCommerce REST API that this connector depends on.

These are pinned deliberately. Each one is a place where a plausible-looking
implementation is wrong against the real store, and where the bug would only
appear in production against a merchant's live shop.
"""

from __future__ import annotations

import httpx
import pytest

from woocommerce_connector import query as q
from woocommerce_connector.auth import Credentials
from woocommerce_connector.client import WooClient, _map_client_error
from woocommerce_connector.errors import AuthError, NotFound, PermissionDenied
from woocommerce_connector.normalize import ORDER_URL_HPOS, ORDER_URL_LEGACY, order_url

CREDS = Credentials("https://shop.example.com", "ck_x", "cs_y")


def test_namespace_is_wc_v3():
    """WooCommerce lives under /wp-json/wc/v3, not /api or /wc-api."""
    assert CREDS.api_base().endswith("/wp-json/wc/v3")


def test_per_page_hard_cap_is_100():
    """WooCommerce rejects per_page > 100 with a 400. We stop it client side."""
    assert q.MAX_PER_PAGE == 100


def test_pagination_uses_wp_headers_not_a_body_envelope():
    """Unlike most APIs, WooCommerce returns a bare JSON array and puts the
    paging metadata in X-WP-Total / X-WP-TotalPages / Link."""
    def handler(request):
        return httpx.Response(
            200, json=[{"id": 1}],
            headers={"X-WP-Total": "57", "X-WP-TotalPages": "6",
                     "Link": '<https://shop.example.com/wp-json/wc/v3/orders?page=2>; rel="next"'},
        )

    async def run():
        c = WooClient(CREDS, transport=httpx.MockTransport(handler))
        data, info = await c.get_page("/orders")
        await c.aclose()
        return data, info

    import asyncio
    data, info = asyncio.run(run())
    assert isinstance(data, list)
    assert info == {"total": 57, "total_pages": 6, "has_more": True}


def test_error_envelope_shape():
    """`{"code", "message", "data": {"status"}}` is WooCommerce's envelope."""
    resp = httpx.Response(404, json={"code": "woocommerce_rest_invalid_id",
                                     "message": "Invalid ID.", "data": {"status": 404}})
    err = _map_client_error(resp)
    assert isinstance(err, NotFound)
    assert err.details["woocommerce_code"] == "woocommerce_rest_invalid_id"


def test_cannot_view_is_permission_not_auth_even_at_401():
    """Hosts disagree on the status for woocommerce_rest_cannot_view: some
    send 401, some 403. The code is what distinguishes a scoped-out key from
    a revoked one, so we branch on the code first."""
    for status in (401, 403):
        resp = httpx.Response(status, json={"code": "woocommerce_rest_cannot_view",
                                            "message": "Sorry, you cannot view this resource.",
                                            "data": {"status": status}})
        assert isinstance(_map_client_error(resp), PermissionDenied)


def test_bad_credentials_map_to_auth_error():
    resp = httpx.Response(401, json={"code": "woocommerce_rest_authentication_error",
                                     "message": "Consumer key is invalid.",
                                     "data": {"status": 401}})
    assert isinstance(_map_client_error(resp), AuthError)


def test_guest_orders_have_customer_id_zero():
    """Guest checkouts are customer_id 0, not null. A truthiness check would
    silently treat every guest order as belonging to customer 0."""
    from mock_server import data as mock_data
    guests = [o for o in mock_data.ORDERS if o["customer_id"] == 0]
    assert guests, "fixture must contain guest orders"
    assert all(o["customer_id"] is not None for o in guests)


def test_hpos_and_legacy_admin_urls_differ():
    """WooCommerce 8.2+ moved orders out of wp_posts. The legacy post.php
    link 404s on an HPOS store, so the style must be configurable."""
    assert "page=wc-orders" in ORDER_URL_HPOS
    assert "post.php" in ORDER_URL_LEGACY
    assert order_url("https://s.example.com", 7) != order_url("https://s.example.com", 7, hpos=False)


def test_customer_role_defaults_to_all():
    """Customers who checked out without registering have no WP role; the
    default role filter would hide them."""
    assert q.build_customer_query()["role"] == "all"


def test_order_date_filters_are_site_local_iso_without_timezone():
    """WooCommerce's after/before compare against the site timezone and
    reject a trailing Z on some versions, so we send a naive timestamp."""
    out = q.to_iso8601("2026-03-04T11:22:33Z", "after")
    assert out == "2026-03-04T11:22:33"
    assert not out.endswith("Z")


@pytest.mark.parametrize("status", ["trash", "checkout-draft"])
def test_internal_statuses_are_not_exposed(status):
    """An agent must never quote a deleted order or an abandoned draft."""
    assert status not in q.ORDER_STATUSES
