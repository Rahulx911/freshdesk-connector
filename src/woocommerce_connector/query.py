"""Typed filters -> WooCommerce REST query parameters.

The model never writes query syntax. It passes typed, documented arguments
and this module turns them into `wc/v3` parameters, rejecting anything not on
an allow-list. That keeps a prompt-injected "search for ?role=administrator"
from becoming a real request, and it means a bad argument fails here with a
fixable message rather than as an opaque 400 from the store.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

from .errors import InvalidRequest

# WooCommerce core order statuses, plus "any". `trash` and `checkout-draft`
# are deliberately excluded: an agent answering a customer should not be
# quoting abandoned drafts or deleted orders.
ORDER_STATUSES = (
    "any", "pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed",
)
PRODUCT_STATUSES = ("any", "draft", "pending", "private", "publish")
STOCK_STATUSES = ("instock", "outofstock", "onbackorder")
ORDER_ORDERBY = ("date", "id", "modified", "title", "include")
PRODUCT_ORDERBY = ("date", "id", "title", "slug", "price", "popularity", "rating", "modified")
SORT_DIRECTIONS = ("asc", "desc")

MAX_PER_PAGE = 100          # WooCommerce hard cap
DEFAULT_PER_PAGE = 20
MAX_SEARCH_LEN = 120

# Search text is passed to WordPress as a `search` parameter. Allow ordinary
# product/customer/order text only; no control characters, no angle brackets.
_SEARCH_OK = re.compile(rf"^[\w\s@.,'&()\-/#+]{{1,{MAX_SEARCH_LEN}}}$", re.UNICODE)


def _one_of(value: str, allowed: tuple[str, ...], field: str) -> str:
    v = (value or "").strip().lower()
    if v not in allowed:
        raise InvalidRequest(
            f"{field}={value!r} is not supported",
            details={"allowed": list(allowed)},
        )
    return v


def clean_statuses(values: list[str] | None, allowed: tuple[str, ...], field: str) -> str | None:
    """WooCommerce takes a comma separated list; `any` wins over everything."""
    if not values:
        return None
    cleaned = [_one_of(v, allowed, field) for v in values]
    if "any" in cleaned:
        return "any"
    # de-duplicate, keep caller order
    seen: list[str] = []
    for c in cleaned:
        if c not in seen:
            seen.append(c)
    return ",".join(seen)


def clean_per_page(value: int | None) -> int:
    if value is None:
        return DEFAULT_PER_PAGE
    try:
        n = int(value)
    except (TypeError, ValueError) as e:
        raise InvalidRequest(f"per_page={value!r} is not a number") from e
    if n < 1 or n > MAX_PER_PAGE:
        raise InvalidRequest(
            f"per_page must be between 1 and {MAX_PER_PAGE}",
            details={"requested": n, "max": MAX_PER_PAGE},
        )
    return n


def clean_page(value: int | None) -> int:
    if value is None:
        return 1
    try:
        n = int(value)
    except (TypeError, ValueError) as e:
        raise InvalidRequest(f"page={value!r} is not a number") from e
    if n < 1:
        raise InvalidRequest("page must be 1 or greater")
    return n


def clean_id(value: Any, field: str = "id") -> int:
    try:
        n = int(value)
    except (TypeError, ValueError) as e:
        raise InvalidRequest(f"{field}={value!r} is not a numeric id") from e
    if n < 1:
        raise InvalidRequest(f"{field} must be a positive integer")
    return n


def clean_search(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    if len(v) > MAX_SEARCH_LEN:
        raise InvalidRequest(f"search text is longer than {MAX_SEARCH_LEN} characters")
    if not _SEARCH_OK.match(v):
        raise InvalidRequest(
            "search text contains characters that are not allowed",
            details={"allowed": "letters, digits, spaces and @ . , ' & ( ) - / # +"},
        )
    return v


def to_iso8601(value: str | None, field: str) -> str | None:
    """WooCommerce wants ISO 8601 in the *site's* timezone for after/before.

    We accept a plain date or a full timestamp and always send a full
    timestamp, because WooCommerce treats a bare date as midnight and silently
    drops same-day orders otherwise.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        if len(raw) == 10:
            d = date.fromisoformat(raw)
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        cleaned = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError as e:
        raise InvalidRequest(
            f"{field}={value!r} is not a valid date",
            details={"expected": "YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ"},
        ) from e


def build_order_query(
    *,
    status: list[str] | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    modified_after: str | None = None,
    customer_id: int | None = None,
    product_id: int | None = None,
    search: str | None = None,
    order_by: str | None = None,
    direction: str | None = None,
    page: int | None = None,
    per_page: int | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "page": clean_page(page),
        "per_page": clean_per_page(per_page),
        "orderby": _one_of(order_by, ORDER_ORDERBY, "order_by") if order_by else "date",
        "order": _one_of(direction, SORT_DIRECTIONS, "direction") if direction else "desc",
    }
    st = clean_statuses(status, ORDER_STATUSES, "status")
    if st:
        params["status"] = st
    if created_after:
        params["after"] = to_iso8601(created_after, "created_after")
    if created_before:
        params["before"] = to_iso8601(created_before, "created_before")
    if modified_after:
        params["modified_after"] = to_iso8601(modified_after, "modified_after")
    if customer_id is not None:
        params["customer"] = clean_id(customer_id, "customer_id")
    if product_id is not None:
        params["product"] = clean_id(product_id, "product_id")
    s = clean_search(search)
    if s:
        params["search"] = s
    return params


def build_product_query(
    *,
    status: list[str] | None = None,
    stock_status: str | None = None,
    search: str | None = None,
    sku: str | None = None,
    order_by: str | None = None,
    direction: str | None = None,
    page: int | None = None,
    per_page: int | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "page": clean_page(page),
        "per_page": clean_per_page(per_page),
        "orderby": _one_of(order_by, PRODUCT_ORDERBY, "order_by") if order_by else "date",
        "order": _one_of(direction, SORT_DIRECTIONS, "direction") if direction else "desc",
    }
    st = clean_statuses(status, PRODUCT_STATUSES, "status")
    if st:
        params["status"] = st
    if stock_status:
        params["stock_status"] = _one_of(stock_status, STOCK_STATUSES, "stock_status")
    s = clean_search(search)
    if s:
        params["search"] = s
    if sku:
        cleaned_sku = clean_search(sku)
        if cleaned_sku:
            params["sku"] = cleaned_sku
    return params


def build_customer_query(
    *,
    search: str | None = None,
    email: str | None = None,
    order_by: str | None = None,
    direction: str | None = None,
    page: int | None = None,
    per_page: int | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "page": clean_page(page),
        "per_page": clean_per_page(per_page),
        "orderby": _one_of(order_by, ("id", "include", "name", "registered_date"), "order_by")
        if order_by else "registered_date",
        "order": _one_of(direction, SORT_DIRECTIONS, "direction") if direction else "desc",
        "role": "all",   # customers created at checkout have no WP role
    }
    if email:
        e = email.strip()
        if "@" not in e or len(e) > 254 or not _SEARCH_OK.match(e):
            raise InvalidRequest(f"email={email!r} is not a valid address")
        params["email"] = e
    s = clean_search(search)
    if s:
        params["search"] = s
    return params
