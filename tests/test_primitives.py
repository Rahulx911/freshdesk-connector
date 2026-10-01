from __future__ import annotations

import pytest

from woocommerce_connector.errors import InvalidRequest, NotFound


async def test_list_orders_pages_and_reports_total(service):
    out = await service.list_orders(per_page=4)
    assert len(out["items"]) == 4
    assert out["total_matching"] == 10
    assert out["has_more"] is True
    assert out["next_page"] == 2
    page2 = await service.list_orders(per_page=4, page=2)
    assert {o["id"] for o in page2["items"]}.isdisjoint({o["id"] for o in out["items"]})


async def test_orders_are_newest_first(service):
    items = (await service.list_orders(per_page=10))["items"]
    dates = [o["date_created"] for o in items]
    assert dates == sorted(dates, reverse=True)


async def test_search_by_email_finds_guest_order(service):
    out = await service.search_orders(search="rohit.bansal@example.com", status=["any"])
    assert [o["id"] for o in out["items"]] == [1104]


async def test_status_filter(service):
    out = await service.search_orders(status=["failed", "pending"])
    assert {o["status"] for o in out["items"]} == {"failed", "pending"}


async def test_get_order_includes_items_and_refund_rows(service):
    o = await service.get_order(1106)
    assert o["id"] == 1106
    assert len(o["line_items"]) == 2
    assert o["refunds"][0]["amount"] == "750.00"


async def test_get_order_unknown_id(service):
    with pytest.raises(NotFound):
        await service.get_order(999999)


async def test_bad_id_is_rejected_before_any_request(service):
    with pytest.raises(InvalidRequest):
        await service.get_order("not-a-number")


async def test_products_and_stock_filter(service):
    out = await service.list_products(stock_status="outofstock")
    assert [p["sku"] for p in out["items"]] == ["KL-AG-500"]


async def test_customer_lookup_by_email(service):
    out = await service.find_customers(email="ananya.rao@example.com")
    assert out["items"][0]["id"] == 31


async def test_customer_history_for_registered_account(service):
    out = await service.customer_order_history(customer_id=31)
    assert out["matched_by"] == "customer_id"
    assert {o["id"] for o in out["orders"]} == {1101, 1106}
    assert out["summary"]["orders"] == 2


async def test_customer_history_falls_back_to_guest_orders(service):
    out = await service.customer_order_history(email="rohit.bansal@example.com")
    assert out["matched_by"] == "guest_email"
    assert out["customer"] is None
    assert [o["id"] for o in out["orders"]] == [1104]
    assert "guest" in out["note"].lower()


async def test_customer_history_needs_an_argument(service):
    with pytest.raises(InvalidRequest):
        await service.customer_order_history()


async def test_refunds_endpoint(service):
    out = await service.list_order_refunds(1102)
    assert out["order_id"] == 1102
    assert out["items"][0]["amount"] == "2450.00"


async def test_connector_status_reports_budget(service):
    st = await service.connector_status()
    assert st["connected"] is True
    assert st["orders_visible"] == 10
    assert st["rate_budget"]["limit_per_min"] > 0
    assert "read-only" in st["mode"]
