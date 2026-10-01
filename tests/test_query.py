from __future__ import annotations

import pytest

from woocommerce_connector import query as q
from woocommerce_connector.errors import InvalidRequest


def test_status_list_becomes_comma_separated():
    p = q.build_order_query(status=["failed", "pending"])
    assert p["status"] == "failed,pending"


def test_any_status_wins():
    assert q.build_order_query(status=["failed", "any"])["status"] == "any"


def test_duplicate_statuses_collapse():
    assert q.build_order_query(status=["failed", "failed"])["status"] == "failed"


@pytest.mark.parametrize("bad", ["trash", "checkout-draft", "deleted", "'; DROP TABLE", "any' OR 1=1"])
def test_unknown_status_rejected(bad):
    with pytest.raises(InvalidRequest):
        q.build_order_query(status=[bad])


@pytest.mark.parametrize("bad", [0, -1, 101, 1000, "ten"])
def test_per_page_bounds(bad):
    with pytest.raises(InvalidRequest):
        q.clean_per_page(bad)


def test_per_page_cap_matches_woocommerce():
    assert q.MAX_PER_PAGE == 100
    assert q.clean_per_page(100) == 100


def test_bare_date_becomes_a_full_timestamp():
    """WooCommerce treats a bare date as midnight; sending the date alone
    silently drops same-day orders."""
    assert q.to_iso8601("2026-03-04", "after") == "2026-03-04T00:00:00"
    assert q.to_iso8601("2026-03-04T11:22:33Z", "after") == "2026-03-04T11:22:33"


@pytest.mark.parametrize("bad", ["yesterday", "2026-13-01", "04/03/2026", "soon"])
def test_bad_dates_rejected(bad):
    with pytest.raises(InvalidRequest):
        q.to_iso8601(bad, "after")


@pytest.mark.parametrize("bad", [
    "<script>alert(1)</script>",
    "status=any&role=administrator",
    "a" * 200,
    "drop\x00table",
])
def test_search_text_allow_list(bad):
    with pytest.raises(InvalidRequest):
        q.clean_search(bad)


@pytest.mark.parametrize("ok", ["ananya.rao@example.com", "KL-NB-250", "order #1101", "Rao & Sons"])
def test_search_text_accepts_ordinary_input(ok):
    assert q.clean_search(ok) == ok


def test_customer_query_includes_all_roles():
    """Customers created at checkout have no WP role; without role=all the
    store returns an empty list and the agent wrongly reports no account."""
    assert q.build_customer_query()["role"] == "all"


def test_invalid_email_rejected():
    with pytest.raises(InvalidRequest):
        q.build_customer_query(email="not-an-email")
