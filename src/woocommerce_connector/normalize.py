"""Turn raw WooCommerce JSON into compact, LLM-shaped records.

Three jobs:

1. **Shrink.** A WooCommerce order is ~80 fields of which an agent needs
   maybe 20. Sending the rest wastes context and invites the model to quote
   internal ids at customers.
2. **Protect.** Billing email, phone and address are masked unless the
   merchant's configuration says otherwise. Customer IP and user agent are
   never returned at all.
3. **Cite.** Every record carries a `source_url` deep link into wp-admin so a
   human can check the answer in one click.
"""

from __future__ import annotations

import re
from typing import Any

from .insights import Signals, derive

# WooCommerce 8.2+ stores orders in its own tables (HPOS) and the admin URL
# changed. The legacy post.php link 404s on an HPOS store and vice versa, so
# the link style is configurable and defaults to HPOS, which is the default
# for new stores.
ORDER_URL_HPOS = "{store}/wp-admin/admin.php?page=wc-orders&action=edit&id={id}"
ORDER_URL_LEGACY = "{store}/wp-admin/post.php?post={id}&action=edit"
PRODUCT_URL = "{store}/wp-admin/post.php?post={id}&action=edit"
CUSTOMER_URL = "{store}/wp-admin/user-edit.php?user_id={id}"

_EMAIL_RE = re.compile(r"^([^@]+)@(.+)$")


def mask_email(value: str | None) -> str | None:
    """a.customer@example.com -> a***r@example.com (domain kept: it is useful
    for routing and is not personally identifying on its own)."""
    if not value:
        return None
    m = _EMAIL_RE.match(value.strip())
    if not m:
        return "***"
    local, domain = m.group(1), m.group(2)
    if len(local) <= 2:
        masked = local[0] + "*" if local else "*"
    else:
        masked = f"{local[0]}{'*' * min(3, len(local) - 2)}{local[-1]}"
    return f"{masked}@{domain}"


def mask_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) < 4:
        return "***"
    return f"{'*' * (len(digits) - 4)}{digits[-4:]}"


def mask_name(value: str | None) -> str | None:
    if not value:
        return None
    parts = [p for p in value.strip().split() if p]
    if not parts:
        return None
    return " ".join([parts[0]] + [f"{p[0]}." for p in parts[1:]])


def _address(block: dict | None, *, redact: bool) -> dict | None:
    if not isinstance(block, dict):
        return None
    out = {
        "city": block.get("city") or None,
        "state": block.get("state") or None,
        "postcode": block.get("postcode") or None,
        "country": block.get("country") or None,
    }
    name = " ".join(x for x in (block.get("first_name"), block.get("last_name")) if x).strip()
    out["name"] = mask_name(name) if redact else (name or None)
    email, phone = block.get("email"), block.get("phone")
    out["email"] = mask_email(email) if redact else (email or None)
    out["phone"] = mask_phone(phone) if redact else (phone or None)
    if not redact:
        out["address_line"] = " ".join(
            x for x in (block.get("address_1"), block.get("address_2")) if x
        ).strip() or None
    return {k: v for k, v in out.items() if v}


def _line_items(items: Any, limit: int = 20) -> tuple[list[dict], int]:
    rows = [i for i in (items or []) if isinstance(i, dict)]
    shown = [
        {k: v for k, v in {
            "product_id": i.get("product_id"),
            "name": i.get("name"),
            "sku": i.get("sku") or None,
            "quantity": i.get("quantity"),
            "total": i.get("total"),
        }.items() if v not in (None, "")}
        for i in rows[:limit]
    ]
    return shown, max(0, len(rows) - limit)


def order_url(store_url: str, order_id: Any, *, hpos: bool = True) -> str:
    tpl = ORDER_URL_HPOS if hpos else ORDER_URL_LEGACY
    return tpl.format(store=store_url.rstrip("/"), id=order_id)


def normalize_order(
    raw: dict,
    *,
    store_url: str,
    redact_pii: bool = True,
    hpos: bool = True,
    include_items: bool = True,
    signals: Signals | None = None,
) -> dict:
    sig = signals or derive(raw)
    out: dict[str, Any] = {
        "id": raw.get("id"),
        "number": raw.get("number"),
        "status": raw.get("status"),
        "currency": raw.get("currency"),
        "total": raw.get("total"),
        "date_created": raw.get("date_created_gmt") or raw.get("date_created"),
        "date_paid": raw.get("date_paid_gmt") or raw.get("date_paid"),
        "date_modified": raw.get("date_modified_gmt") or raw.get("date_modified"),
        "payment_method_title": raw.get("payment_method_title") or None,
        "customer_id": raw.get("customer_id") or None,
        "source_url": order_url(store_url, raw.get("id"), hpos=hpos),
    }
    billing = _address(raw.get("billing"), redact=redact_pii)
    if billing:
        out["billing"] = billing
    ship = _address(raw.get("shipping"), redact=redact_pii)
    if ship and ship != billing:
        out["shipping"] = ship
    if include_items:
        items, more = _line_items(raw.get("line_items"))
        if items:
            out["line_items"] = items
        if more:
            out["line_items_truncated"] = more
    note = (raw.get("customer_note") or "").strip()
    if note:
        out["customer_note"] = note
    out["signals"] = sig.to_dict()
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


def normalize_refund(raw: dict) -> dict:
    return {k: v for k, v in {
        "id": raw.get("id"),
        "date_created": raw.get("date_created_gmt") or raw.get("date_created"),
        "amount": raw.get("amount") or raw.get("total"),
        "reason": (raw.get("reason") or "").strip() or None,
        "refunded_by": raw.get("refunded_by") or None,
    }.items() if v not in (None, "")}


def normalize_product(raw: dict, *, store_url: str) -> dict:
    return {k: v for k, v in {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "sku": raw.get("sku") or None,
        "status": raw.get("status"),
        "price": raw.get("price") or None,
        "regular_price": raw.get("regular_price") or None,
        "sale_price": raw.get("sale_price") or None,
        "stock_status": raw.get("stock_status"),
        "stock_quantity": raw.get("stock_quantity"),
        "total_sales": raw.get("total_sales"),
        "source_url": PRODUCT_URL.format(store=store_url.rstrip("/"), id=raw.get("id")),
    }.items() if v not in (None, "")}


def normalize_customer(raw: dict, *, store_url: str, redact_pii: bool = True) -> dict:
    name = " ".join(x for x in (raw.get("first_name"), raw.get("last_name")) if x).strip()
    out = {
        "id": raw.get("id"),
        "name": (mask_name(name) if redact_pii else name) or None,
        "email": mask_email(raw.get("email")) if redact_pii else (raw.get("email") or None),
        "username": raw.get("username") or None,
        "date_created": raw.get("date_created_gmt") or raw.get("date_created"),
        "orders_count": raw.get("orders_count"),
        "total_spent": raw.get("total_spent"),
        "source_url": CUSTOMER_URL.format(store=store_url.rstrip("/"), id=raw.get("id")),
    }
    billing = _address(raw.get("billing"), redact=redact_pii)
    if billing:
        out["billing"] = billing
    return {k: v for k, v in out.items() if v not in (None, "")}
